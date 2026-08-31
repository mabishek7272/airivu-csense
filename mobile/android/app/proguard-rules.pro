# kotlinx.serialization + Retrofit models - keep the generated serializers reachable.
-keepattributes *Annotation*, InnerClasses
-dontnote kotlinx.serialization.AnnotationsKt

-keepclassmembers class kotlinx.serialization.json.** {
    *** Companion;
}
-keepclasseswithmembers class kotlinx.serialization.json.** {
    kotlinx.serialization.KSerializer serializer(...);
}
-keep,includedescriptorclasses class ai.airivu.csense.**$$serializer { *; }
-keepclassmembers class ai.airivu.csense.** {
    *** Companion;
}
-keepclasseswithmembers class ai.airivu.csense.** {
    kotlinx.serialization.KSerializer serializer(...);
}

# Retrofit/OkHttp: keep response/request bodies' generic signatures.
-keepattributes Signature, Exceptions
-dontwarn okhttp3.**
-dontwarn retrofit2.**
