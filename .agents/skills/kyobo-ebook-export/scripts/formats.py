"""Local format validation and lossless resource packaging. No network access."""
import hashlib
import html.entities
import json
import posixpath
import re
import subprocess
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from urllib.parse import unquote, urlsplit


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def parse_xml(data):
    try:
        return ET.fromstring(data)
    except ET.ParseError:
        # Validation-only entity expansion; publication bytes are never rewritten.
        def replace(m):
            name = m.group(1)
            if name in ('amp', 'lt', 'gt', 'quot', 'apos'):
                return m.group(0)
            value = html.entities.html5.get(name + ';')
            return ''.join(f'&#{ord(c)};' for c in value) if value else m.group(0)
        return ET.fromstring(re.sub(r'&([A-Za-z][A-Za-z0-9]+);', replace, data.decode('utf-8-sig')))


def local_reference(base, href):
    url = urlsplit(href)
    if url.scheme or url.netloc:
        raise ValueError('Remote publication resource is not available offline')
    decoded = unquote(url.path)
    result = posixpath.normpath(posixpath.join(posixpath.dirname(base), decoded))
    if result.startswith(('../', '/')) or result == '..' or '\\' in result or '\0' in result:
        raise ValueError('Publication reference escapes its root')
    return result


def epub_members(root):
    root = Path(root)
    if (root / 'mimetype').read_bytes() != b'application/epub+zip':
        raise ValueError('Invalid EPUB mimetype')
    container = parse_xml((root / 'META-INF/container.xml').read_bytes())
    package_paths = [local_reference('', n.attrib['full-path']) for n in container.iter() if n.tag.endswith('rootfile')]
    if len(package_paths) != 1:
        raise ValueError('Expected one EPUB package document')
    opf_path = package_paths[0]
    opf = parse_xml((root / opf_path).read_bytes())
    manifest = {n.attrib['id']: n.attrib for n in opf.iter() if n.tag.endswith('}item')}
    spine = [n.attrib['idref'] for n in opf.iter() if n.tag.endswith('}itemref')]
    if not manifest or not spine or not all(i in manifest for i in spine):
        raise ValueError('Incomplete EPUB manifest/spine')
    names = {'mimetype', 'META-INF/container.xml', opf_path}
    names.update(local_reference(opf_path, item['href']) for item in manifest.values())
    names.update(str(p.relative_to(root)) for p in (root / 'META-INF').rglob('*') if p.is_file())
    if 'META-INF/encryption.xml' in names:
        raise ValueError('EPUB encryption manifest needs a separately verified adapter')
    for name in names:
        p = root / name
        if not p.resolve().is_relative_to(root.resolve()) or p.is_symlink() or not p.is_file():
            raise ValueError('Missing or unsafe publication resource: ' + name)
    return sorted(names), opf_path, opf, manifest, spine


def package_epub(root, output, validator, work, command):
    root, output, work = Path(root), Path(output), Path(work)
    names, opf_path, opf, manifest, spine = epub_members(root)
    warnings, images = [], []
    chars = hangul = markup = xhtml = 0
    resources = {}
    for name in names:
        p = root / name
        data = p.read_bytes()
        resources[name] = hashlib.sha256(data).hexdigest()
        ext = p.suffix.lower()
        if ext in ('.xhtml', '.html', '.htm', '.xml', '.opf', '.ncx', '.svg'):
            doc = parse_xml(data)
            markup += 1
            if ext in ('.xhtml', '.html', '.htm'):
                if not doc.tag.endswith('html'):
                    raise ValueError('Decrypted XHTML has no HTML root')
                xhtml += 1
                text = ''.join(doc.itertext())
                chars += len(text)
                hangul += sum('\uac00' <= c <= '\ud7a3' for c in text)
                if any(n.tag.rsplit('}', 1)[-1] in ('ul', 'ol') and any(c.tag.rsplit('}', 1)[-1] != 'li' for c in n) for n in doc.iter()):
                    warnings.append({'code': 'list_child_structure', 'member': name})
                if any(n.tag.rsplit('}', 1)[-1] == 'head' and not any(c.tag.rsplit('}', 1)[-1] == 'title' for c in n) for n in doc.iter()):
                    warnings.append({'code': 'missing_html_title', 'member': name})
            if ext == '.opf' and any(n.tag.rsplit('}', 1)[-1] == 'guide' and len(n) == 0 for n in doc.iter()):
                warnings.append({'code': 'empty_opf_guide', 'member': name})
        if ext in ('.png', '.jpg', '.jpeg', '.gif'):
            images.append(str(p))
        if ext in ('.ttf', '.otf', '.ttc') and data[:4] not in (b'OTTO', b'ttcf', b'\x00\x01\x00\x00', b'true'):
            raise ValueError('Unrecognized or still-protected font resource')
    if xhtml < len(spine) or chars == 0:
        raise ValueError('Missing EPUB body text')
    image_input, image_report = work / 'images.json', work / 'image-validation.json'
    image_input.write_text(json.dumps(images))
    command([str(validator), 'images', str(image_input), str(image_report)], 'images', timeout=180)
    image_result = json.loads(image_report.read_text())
    with zipfile.ZipFile(output, 'x', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        z.writestr('mimetype', b'application/epub+zip', compress_type=zipfile.ZIP_STORED)
        for name in sorted(set(names) - {'mimetype'}):
            z.write(root / name, name)
    with zipfile.ZipFile(output) as z:
        if z.testzip() is not None or z.infolist()[0].compress_type != zipfile.ZIP_STORED:
            raise ValueError('EPUB ZIP verification failed')
        for name, expected in resources.items():
            if hashlib.sha256(z.read(name)).hexdigest() != expected:
                raise ValueError('EPUB resource changed while packaging')
    return {'format': 'epub', 'version': opf.attrib.get('version'), 'spine_documents': len(spine),
            'manifest_resources': len(manifest), 'markup_documents': markup, 'text_characters': chars,
            'hangul_characters': hangul, 'images': image_result, 'warnings': warnings,
            'resource_bytes_preserved': True, 'zip_crc_passed': True,
            'validation_scope': 'local structure, XML, resources, images; not full EPUBCheck conformance'}


def validate_pdf(path, validator, work, command):
    path, work = Path(path), Path(work)
    info = command(['pdfinfo', str(path)], 'pdfinfo', timeout=45).stdout.decode('utf-8', 'replace')
    match = re.search(r'^Pages:\s+(\d+)', info, re.M)
    if not match or not re.search(r'^Encrypted:\s+no', info, re.M):
        raise ValueError('PDF parsing or encryption check failed')
    pages = int(match[1])
    extracted = command(['pdftotext', '-enc', 'UTF-8', str(path), '-'], 'pdftotext', timeout=180, retain_stdout=False)
    text = extracted.stdout.decode('utf-8')
    chunks = text.split('\f')
    if not chunks[-1].strip():
        chunks.pop()
    if len(chunks) != pages:
        raise ValueError('PDF text extraction page count differs')
    report = work / 'pdfkit.json'
    command([str(validator), 'pdf', str(path), str(report)], 'pdfkit', timeout=300)
    apple = json.loads(report.read_text())
    if apple['pages'] != pages or apple['rendered_pages'] != pages:
        raise ValueError('PDFKit page validation differs')
    return {'format': 'pdf', 'pages': pages, 'encrypted': False, 'text_characters': len(text),
            'replacement_characters': text.count('\ufffd'), 'pdfkit': apple,
            'poppler_warnings': extracted.stderr.decode('utf-8', 'replace').splitlines()}
