"""Capture only compiler/build variables; never dump an arbitrary container environment."""
import json
import os
import re
import sys

ALLOWLIST = frozenset({
    'TARGET', 'VARIANT', 'ADDINS_STR', 'FFVER', 'SOURCE_DATE_EPOCH', 'TZ', 'LC_ALL',
    'CC', 'CXX', 'LD', 'AR', 'RANLIB', 'NM', 'DLLTOOL', 'GENDEF', 'STRIP',
    'CFLAGS', 'CXXFLAGS', 'LDFLAGS', 'HOST_CC', 'HOST_CXX', 'HOST_CFLAGS', 'HOST_CXXFLAGS',
    'STAGE_CFLAGS', 'STAGE_CXXFLAGS', 'STAGE_LDFLAGS', 'PKG_CONFIG', 'PKG_CONFIG_LIBDIR',
    'FFBUILD_TOOLCHAIN', 'FFBUILD_RUST_TARGET', 'FFBUILD_TARGET_FLAGS', 'FFBUILD_CROSS_PREFIX',
    'FFBUILD_PREFIX', 'FFBUILD_DESTDIR', 'FFBUILD_DESTPREFIX', 'FFBUILD_CMAKE_TOOLCHAIN',
    'FF_CONFIGURE', 'FF_CFLAGS', 'FF_CXXFLAGS', 'FF_LDFLAGS', 'FF_LDEXEFLAGS', 'FF_LIBS',
})
SECRET_VALUE = re.compile(
    r'-----BEGIN (?:[A-Z ]*PRIVATE KEY|OPENSSH PRIVATE KEY)-----|'
    r'\bBearer\s+[A-Za-z0-9._~-]{8,}|'
    r'\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b|'
    r'(?:API[_-]?KEY|ACCESS[_-]?TOKEN|CLIENT[_-]?SECRET|PASSWORD|PASSWD)\s*[=:]|'
    r'https?://[^/\s]+:[^/\s]+@|'
    r'[?&](?:token|access_token|api_key|signature|x-amz-signature)=[^\s&]+',
    re.IGNORECASE,
)

def capture(environment):
    result = {}
    for name in sorted(ALLOWLIST):
        if name not in environment:
            continue
        value = environment[name]
        if '\x00' in value or '\n' in value or '\r' in value or SECRET_VALUE.search(value):
            # Do not print the rejected value in the exception or compiler log.
            raise ValueError('Potential secret in build variable: ' + name)
        result[name] = value
    return result

if __name__ == '__main__':
    json.dump(capture(os.environ), sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write('\n')
