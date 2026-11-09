#import <Foundation/Foundation.h>
#import <Security/Security.h>
#import <objc/runtime.h>
#import <objc/message.h>
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sqlite3.h>
#include <time.h>
#include <unistd.h>

static time_t replayTime=0;
static BOOL owned=NO;
static void logEvent(NSString *event,NSDictionary *fields){
    const char *path=getenv("KY_LOG");if(!path)return;
    NSMutableDictionary *row=[NSMutableDictionary dictionaryWithDictionary:fields?:@{}];row[@"event"]=event;
    NSData *data=[NSJSONSerialization dataWithJSONObject:row options:0 error:NULL];
    FILE *file=fopen(path,"a");if(file){fwrite(data.bytes,1,data.length,file);fputc('\n',file);fclose(file);}
}
static OSStatus blockAdd(CFDictionaryRef q,CFTypeRef *r){logEvent(@"keychain_write_blocked",@{@"operation":@"add"});return errSecInteractionNotAllowed;}
static OSStatus blockUpdate(CFDictionaryRef q,CFDictionaryRef a){logEvent(@"keychain_write_blocked",@{@"operation":@"update"});return errSecInteractionNotAllowed;}
static OSStatus blockDelete(CFDictionaryRef q){logEvent(@"keychain_write_blocked",@{@"operation":@"delete"});return errSecInteractionNotAllowed;}
static OSStatus queryItem(CFDictionaryRef q,CFTypeRef *r){
    NSMutableDictionary *query=[(__bridge NSDictionary *)q mutableCopy];
    query[(__bridge id)kSecUseAuthenticationUI]=(__bridge id)kSecUseAuthenticationUIFail;
    OSStatus status=SecItemCopyMatching((__bridge CFDictionaryRef)query,r);
    logEvent(@"keychain_read",@{@"status":@(status)});return status;
}
static time_t localTime(time_t *output){if(!replayTime)return time(output);if(output)*output=replayTime;return replayTime;}
#define INTERPOSE(replacement,original) __attribute__((used)) static const struct {const void *a;const void *b;} ip_##original __attribute__((section("__DATA,__interpose")))={(const void *)&replacement,(const void *)&original};
INTERPOSE(blockAdd,SecItemAdd)
INTERPOSE(blockUpdate,SecItemUpdate)
INTERPOSE(blockDelete,SecItemDelete)
INTERPOSE(queryItem,SecItemCopyMatching)
INTERPOSE(localTime,time)

static id send0(id o,const char *s){return ((id(*)(id,SEL))objc_msgSend)(o,sel_registerName(s));}
static id send2(id o,const char *s,id a,id b){return ((id(*)(id,SEL,id,id))objc_msgSend)(o,sel_registerName(s),a,b);}
static void void0(id o,const char *s){((void(*)(id,SEL))objc_msgSend)(o,sel_registerName(s));}
static void void1(id o,const char *s,id a){((void(*)(id,SEL,id))objc_msgSend)(o,sel_registerName(s),a);}
static BOOL verifyReaderABI(void){
    struct MethodShape {const char *cls;const char *selector;const char *encoding;BOOL meta;} shapes[]={
        {"DRMManager","sharedInstance","@16@0:8",YES},
        {"DRMManager","domainMap","@16@0:8",NO},
        {"DRMManager","setAuthToken:","v24@0:8@16",NO},
        {"FMAuthenticatedToken","initWithUserIdDeviceIdFromFile:token:","@32@0:8@16@24",NO},
        {"FMAuthenticatedToken","setPassword:","v24@0:8@16",NO},
        {"FMDcfManager","initWithToken:domainMap:","@32@0:8@16@24",NO},
        {"FMDcfManager","getDrmPackagerWithFileName:error:","@32@0:8@16^@24",NO},
        {"FMDcfManager","getLicenseOfSecureHeader:error:","@32@0:8@16^@24",NO},
        {"FMDrmFilePackager","originalFileSize","Q16@0:8",NO},
        {"FMDrmFilePackager","read:","v24@0:8@16",NO},
        {"FMDrmFilePackager","close","v16@0:8",NO},
    };
    for(NSUInteger i=0;i<sizeof(shapes)/sizeof(shapes[0]);i++){
        struct MethodShape s=shapes[i];Class c=objc_getClass(s.cls);SEL selector=sel_registerName(s.selector);
        Method method=s.meta?class_getClassMethod(c,selector):class_getInstanceMethod(c,selector);
        const char *encoding=method?method_getTypeEncoding(method):NULL;
        if(!encoding||strcmp(encoding,s.encoding)){
            logEvent(@"reader_abi_mismatch",@{@"class":@(s.cls),@"selector":@(s.selector),@"actual":encoding?@(encoding):@"missing"});return NO;
        }
    }
    logEvent(@"reader_abi_verified",@{@"methods":@11});return YES;
}
static id (*licenseOriginal)(id,SEL,id,id *);
static id observeLicense(id object,SEL selector,id header,id *error){
    id value=licenseOriginal(object,selector,header,error);
    if(!owned||strcmp(getenv("KY_CLOCK")?:"current","license-window")||![value isKindOfClass:NSString.class])return value;
    NSRegularExpression *regex=[NSRegularExpression regularExpressionWithPattern:@"<(FROM|UNTIL)>([0-9T: Z+./-]{8,40})</(FROM|UNTIL)>" options:NSRegularExpressionCaseInsensitive error:NULL];
    NSDateFormatter *formatter=[NSDateFormatter new];formatter.locale=[[NSLocale alloc] initWithLocaleIdentifier:@"en_US_POSIX"];
    formatter.timeZone=[NSTimeZone timeZoneForSecondsFromGMT:0];formatter.dateFormat=@"HH:mm:ss yyyy-MM-dd";
    NSDate *from=nil,*until=nil;
    for(NSTextCheckingResult *m in [regex matchesInString:value options:0 range:NSMakeRange(0,[value length])]){
        NSString *field=[value substringWithRange:[m rangeAtIndex:1]];
        NSDate *date=[formatter dateFromString:[value substringWithRange:[m rangeAtIndex:2]]];
        if([field isEqualToString:@"FROM"])from=date;else if([field isEqualToString:@"UNTIL"])until=date;
    }
    if(from&&until&&[until timeIntervalSinceDate:from]>120&&time(NULL)>until.timeIntervalSince1970){
        time_t next=(time_t)((from.timeIntervalSince1970+until.timeIntervalSince1970)/2);
        if(replayTime!=next){replayTime=next;logEvent(@"process_local_historical_time",@{@"unix_seconds":@(next),@"system_clock_modified":@NO});}
    }
    return value;
}
static BOOL saveData(NSData *data,NSString *destination){
    NSString *part=[destination stringByAppendingString:@".part"];
    struct stat st;if(lstat(destination.fileSystemRepresentation,&st)==0)return NO;
    int fd=open(part.fileSystemRepresentation,O_CREAT|O_EXCL|O_WRONLY,0600);if(fd<0)return NO;
    NSUInteger offset=0;
    while(offset<data.length){ssize_t n=write(fd,(const char *)data.bytes+offset,data.length-offset);if(n<0&&errno==EINTR)continue;if(n<=0){close(fd);return NO;}offset+=(NSUInteger)n;}
    int sync=fsync(fd);int closed=close(fd);if(sync<0||closed<0)return NO;
    return rename(part.fileSystemRepresentation,destination.fileSystemRepresentation)==0;
}
static NSData *readBookFile(id manager,NSString *path){
    id error=nil;
    id packager=((id(*)(id,SEL,id,id *))objc_msgSend)(manager,sel_registerName("getDrmPackagerWithFileName:error:"),path,&error);
    if(!packager){logEvent(@"packager_failed",@{@"code":@([error code]),@"domain":[error domain]?:@""});return nil;}
    unsigned long long size=((unsigned long long(*)(id,SEL))objc_msgSend)(packager,sel_registerName("originalFileSize"));
    if(!size||size>512000000){void0(packager,"close");logEvent(@"unsupported_size",@{@"bytes":@(size)});return nil;}
    NSMutableData *data=[NSMutableData dataWithLength:(NSUInteger)size];void1(packager,"read:",data);void0(packager,"close");return data;
}
static int exportJob(id manager,NSDictionary *job){
    if([job[@"format"] isEqualToString:@"pdf"]){
        NSData *data=readBookFile(manager,[job[@"root"] stringByAppendingPathComponent:@"book.dat"]);
        if(data.length<5||memcmp(data.bytes,"%PDF-",5)){logEvent(@"not_pdf",nil);return 80;}
        if(!saveData(data,job[@"output"]))return 81;
        logEvent(@"exported",@{@"format":@"pdf",@"bytes":@(data.length)});return 0;
    }
    NSUInteger copied=0,decrypted=0;
    for(NSString *member in job[@"members"]){@autoreleasepool{
        NSString *source=[job[@"root"] stringByAppendingPathComponent:member];
        NSData *data=[NSData dataWithContentsOfFile:source];NSString *ext=member.pathExtension.lowercaseString;
        BOOL textual=[@[@"xhtml",@"html",@"htm",@"xml",@"opf",@"ncx",@"css",@"js",@"svg"] containsObject:ext];
        NSString *text=textual?[[NSString alloc] initWithData:data encoding:NSUTF8StringEncoding]:nil;
        BOOL plaintext=text&&[text rangeOfString:@"\0"].location==NSNotFound;
        if(textual&&!plaintext){
            data=readBookFile(manager,source);text=data?[[NSString alloc] initWithData:data encoding:NSUTF8StringEncoding]:nil;
            if(!text){logEvent(@"epub_resource_failed",@{@"member":member});return 82;}
            if([@[@"xhtml",@"html",@"htm"] containsObject:ext]&&[text rangeOfString:@"<html" options:NSCaseInsensitiveSearch].location==NSNotFound)return 83;
            decrypted++;
        }else copied++;
        NSString *destination=[job[@"output"] stringByAppendingPathComponent:member];
        if(![NSFileManager.defaultManager createDirectoryAtPath:destination.stringByDeletingLastPathComponent withIntermediateDirectories:YES attributes:@{NSFilePosixPermissions:@0700} error:NULL])return 84;
        if(!data||!saveData(data,destination))return 85;
    }}
    logEvent(@"exported",@{@"format":@"epub-resources",@"copied":@(copied),@"decrypted":@(decrypted)});return 0;
}
__attribute__((constructor)) static void startExport(void){@autoreleasepool{@try{
    if(!getenv("KY_JOB")||!getenv("KY_HOME")||!getenv("KY_LOG"))_exit(69);
    NSString *home=@(getenv("KY_HOME"));
    if(![NSHomeDirectory() isEqualToString:home])_exit(70);
    int fd=open(getenv("KY_ORIGINAL_DB"),O_RDONLY);if(fd>=0){close(fd);_exit(71);}if(errno!=EPERM&&errno!=EACCES)_exit(71);
    fd=open(getenv("KY_WRITE_SENTINEL"),O_WRONLY);if(fd>=0){close(fd);_exit(72);}if(errno!=EPERM&&errno!=EACCES)_exit(72);
    int sock=socket(AF_INET,SOCK_STREAM,0);struct sockaddr_in address={.sin_len=sizeof(address),.sin_family=AF_INET,.sin_port=htons(9),.sin_addr={htonl(0x7f000001)}};
    int connection=sock<0?-1:connect(sock,(struct sockaddr *)&address,sizeof(address));int networkError=errno;if(sock>=0)close(sock);
    if(!(connection<0&&(networkError==EPERM||networkError==EACCES)))_exit(73);
    void *library=dlopen(getenv("KY_SAFETY_CLIENT"),RTLD_NOW|RTLD_LOCAL);int(*verify)(void)=library?dlsym(library,"verifyInterposition"):NULL;
    if(!verify||verify()!=0)_exit(74);
    logEvent(@"isolation_verified",@{@"original_read_denied":@YES,@"outside_write_denied":@YES,@"network_denied":@YES,@"keychain_writes_denied":@YES});
    if(!verifyReaderABI())_exit(91);
    NSDictionary *preferences=[NSDictionary dictionaryWithContentsOfFile:[home stringByAppendingPathComponent:@"Library/Preferences/kr.co.kyobobook.iPadB2C.plist"]];
    if(!preferences)_exit(75);[NSUserDefaults.standardUserDefaults setVolatileDomain:preferences forName:NSArgumentDomain];
    NSDictionary *job=[NSJSONSerialization JSONObjectWithData:[NSData dataWithContentsOfFile:@(getenv("KY_JOB"))] options:0 error:NULL];
    sqlite3 *db=NULL;NSString *dbPath=[home stringByAppendingPathComponent:@"Library/Kyobo/data/books.db"];
    if(sqlite3_open_v2(dbPath.UTF8String,&db,SQLITE_OPEN_READONLY,NULL)!=SQLITE_OK)_exit(76);
    sqlite3_stmt *statement=NULL;NSString *uid=nil;
    if(sqlite3_prepare_v2(db,"SELECT user_id,expire_date,service_type FROM books WHERE barcode=? AND book_type=?",-1,&statement,NULL)==SQLITE_OK){
        sqlite3_bind_text(statement,1,[job[@"barcode"] UTF8String],-1,SQLITE_TRANSIENT);
        sqlite3_bind_text(statement,2,[job[@"book_type"] UTF8String],-1,SQLITE_TRANSIENT);
        if(sqlite3_step(statement)==SQLITE_ROW){
            const char *u=(const char *)sqlite3_column_text(statement,0),*e=(const char *)sqlite3_column_text(statement,1);
            if(u)uid=@(u);owned=e&&strcmp(e,"299912312359")==0&&sqlite3_column_int(statement,2)==1;
        }
    }
    sqlite3_finalize(statement);sqlite3_close(db);if(!uid.length||!owned)_exit(77);
    id drm=send0(objc_getClass("DRMManager"),"sharedInstance"),domain=send0(drm,"domainMap");
    id token=send2(send0(objc_getClass("FMAuthenticatedToken"),"alloc"),"initWithUserIdDeviceIdFromFile:token:",uid,uid);
    void1(token,"setPassword:",uid);void1(drm,"setAuthToken:",token);
    if(!token||!domain)_exit(78);
    Method method=class_getInstanceMethod(objc_getClass("FMDcfManager"),sel_registerName("getLicenseOfSecureHeader:error:"));
    if(!method)_exit(79);licenseOriginal=(void *)method_setImplementation(method,(IMP)observeLicense);
    id manager=send2(send0(objc_getClass("FMDcfManager"),"alloc"),"initWithToken:domainMap:",token,domain);
    int result=exportJob(manager,job);fflush(stdout);fflush(stderr);_exit(result);
}@catch(NSException *e){logEvent(@"exception",@{@"name":e.name});_exit(90);}}}
