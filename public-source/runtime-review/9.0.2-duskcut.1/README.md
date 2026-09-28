# Post-build runtime provenance and license review

This is a review of the actual already-compiled DuskCut FFmpeg build
9.0.2-duskcut.1, GitHub run 36355675824, recipe commit 323eb1d63d630b4bd4e8ae4c89650266534731e8.
It does not rewrite the executed input lock or claim that the review documents
were additional compilation inputs. Original executable bytes are unchanged.

The link trace includes five GCC runtime archive names: libatomic.a, libgcc.a,
libgcc_eh.a, libgomp.a and libstdc++.a. The original input notices already covered
libgcc/libgomp/libstdc++; the actual build revealed that libatomic must also be
recorded. The read-only toolchain audit runs with networking disabled inside the
exact original compiler image and records the archive paths, sizes and hashes.
It does not substitute base-image MinGW hashes for the CRT rebuilt by the real
dependency stage. The original complete pinned MinGW source/notices remain in
the unchanged source-input package.

The libatomic evidence is from GCC commit 78d4ac73dd391005b895a6148cd9831e28e1208b. Its gcc/BASE-VER
is 16.2.0, matching the recorded compiler. libatomic/libatomic_i.h identifies
GNU Atomic Library licensing as GPL version 3 or later and explicitly grants
the GCC Runtime Library Exception 3.1. The full original GPL and exception texts
are included. Source-header bytes are preserved, with a .txt suffix for ordinary
license-viewer compatibility. Original source:
https://github.com/gcc-mirror/gcc/blob/78d4ac73dd391005b895a6148cd9831e28e1208b/libatomic/libatomic_i.h

The component-specific basis is the GCC runtime exception for covered target
code produced by ordinary eligible compilation. The controlled build uses GCC
for normal C/C++ compilation and no proprietary intermediate-representation
plugin. This documents the license text relied upon; it is not a blanket legal,
patent or distribution guarantee and does not cure earlier Gyan source records.

compiled-runtime-inspection.json independently parses the real PE imports after
verifying executable hashes, checks them against archived objdump evidence, and
records config/source hashes and runtime linkage. Both original binaries are
unsigned and import only Windows/UCRT system DLLs, not separately installed GCC
or codec DLLs. Optional vendor GPU drivers remain system-provided. Optical-disc
access and DVD CSS libraries are absent; ordinary DVD subtitle/PCM/MPEG2 formats
and the DVD navigation data parser are not decryption and remain usable.

The inspection's historical status says libatomic review is required because it
was emitted before this post-build review. This manifest plus the exact header,
license, exception and same-image archive audit supply that additional record.
All final publication and functional approval remains a separate operator step.
