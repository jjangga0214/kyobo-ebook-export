#import <Foundation/Foundation.h>
#import <Security/Security.h>
// Invalid empty queries cannot modify items even if interposition is unavailable.
int verifyInterposition(void){
    CFDictionaryRef q=(__bridge CFDictionaryRef)@{};
    return SecItemAdd(q,NULL)==errSecInteractionNotAllowed &&
           SecItemUpdate(q,q)==errSecInteractionNotAllowed &&
           SecItemDelete(q)==errSecInteractionNotAllowed ? 0:1;
}
