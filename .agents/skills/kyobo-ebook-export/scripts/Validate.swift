import Foundation
import AppKit
import PDFKit
import ImageIO

let mode=CommandLine.arguments[1]
let input=URL(fileURLWithPath:CommandLine.arguments[2])
let output=URL(fileURLWithPath:CommandLine.arguments[3])
var result=[String:Any]()
var failed=false
if mode=="pdf"{
    guard let doc=PDFDocument(url:input) else{fatalError("PDF_OPEN_FAILED")}
    var rendered=0,characters=0,replacements=0
    var bad=[Int](),blank=[Int]()
    for i in 0..<doc.pageCount{autoreleasepool{
        guard let page=doc.page(at:i) else{bad.append(i+1);return}
        let text=page.string ?? "";characters+=text.count;replacements+=text.unicodeScalars.filter{$0.value==0xfffd}.count
        let box=page.bounds(for:.mediaBox)
        guard box.width>0&&box.height>0 else{bad.append(i+1);return}
        let width=96,height=max(1,Int(Double(width)*box.height/box.width))
        guard height<10000,let ctx=CGContext(data:nil,width:width,height:height,bitsPerComponent:8,bytesPerRow:width*4,space:CGColorSpaceCreateDeviceRGB(),bitmapInfo:CGImageAlphaInfo.premultipliedLast.rawValue) else{bad.append(i+1);return}
        ctx.setFillColor(CGColor(gray:1,alpha:1));ctx.fill(CGRect(x:0,y:0,width:width,height:height))
        ctx.scaleBy(x:CGFloat(width)/box.width,y:CGFloat(height)/box.height);ctx.translateBy(x:-box.minX,y:-box.minY)
        page.draw(with:.mediaBox,to:ctx)
        guard ctx.makeImage() != nil else{bad.append(i+1);return}
        let pixels=ctx.data!.assumingMemoryBound(to:UInt8.self)
        var ink=0
        for j in stride(from:0,to:width*height*4,by:4){if min(pixels[j],pixels[j+1],pixels[j+2])<230{ink+=1}}
        if ink==0{blank.append(i+1)}
        rendered+=1
    }}
    failed = doc.pageCount == 0 || doc.isEncrypted || doc.isLocked || !bad.isEmpty
    result=["pages":doc.pageCount,"rendered_pages":rendered,"render_failures":bad,"blank_renders":blank,"encrypted":doc.isEncrypted,"locked":doc.isLocked,"text_characters":characters,"replacement_characters":replacements]
}else if mode=="images"{
    let paths=try JSONSerialization.jsonObject(with:Data(contentsOf:input)) as! [String]
    var bad=[Int]()
    for (index,path) in paths.enumerated(){autoreleasepool{
        guard let src=CGImageSourceCreateWithURL(URL(fileURLWithPath:path) as CFURL,nil),let image=CGImageSourceCreateImageAtIndex(src,0,[kCGImageSourceShouldCacheImmediately:true] as CFDictionary),image.width>0,image.height>0 else{bad.append(index);return}
    }}
    failed = !bad.isEmpty;result=["images":paths.count,"decoded":paths.count-bad.count,"failed_indices":bad]
}else{fatalError("UNKNOWN_MODE")}
try JSONSerialization.data(withJSONObject:result,options:[.prettyPrinted,.sortedKeys]).write(to:output)
exit(failed ? 1:0)
