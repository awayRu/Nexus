[app]
title = NEXUS
package.name = nexusmessenger
package.domain = org.nexuschat
source.dir = .
source.include_exts = py,png,jpg,jpeg,kv,atlas
source.exclude_dirs = .git,.github,server,deploy,tests,__pycache__,.buildozer,bin
version = 2.0.0
requirements = python3,kivy,requests,certifi,urllib3,websocket-client
orientation = portrait
fullscreen = 0
android.permissions = INTERNET,READ_EXTERNAL_STORAGE,READ_MEDIA_IMAGES
android.api = 35
android.minapi = 24
android.archs = arm64-v8a
android.accept_sdk_license = True

[buildozer]
log_level = 2
warn_on_root = 1
