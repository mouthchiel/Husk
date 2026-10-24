#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Run the production prewarm state machine with mock iOS allocators.

This checks retry, route selection, reuse and concurrent callers on a host.
It does not test Mach mappings, arm64 execution, or the Swift/iOS build.
"""
import os
from pathlib import Path
import shlex
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]
source = (ROOT / "src/ios-jit/husk-ios-jit.c").read_text()
start = source.index("static HuskDualMapping husk_prewarmed;")
end = source.index("/* The held region,", start)
# Compile the actual edited functions rather than a second implementation.
production = source[start:end]
prefix = r'''
#include "husk-ios-jit.h"
#include <assert.h>
#include <pthread.h>
#include <stdio.h>
static bool debugged, self_ok, trap_ok;
static int self_calls, trap_calls;
static uint8_t rw[1], rx[1];
static bool husk_kernel_debugged(void) { return debugged; }
static HuskDualMapping mock_mapping(size_t bytes, bool ok)
{
    HuskDualMapping m = {ok ? rw : NULL, ok ? rx : NULL, ok ? bytes : 0};
    return m;
}
static HuskDualMapping husk_self_dual_mapping(size_t bytes)
{
    ++self_calls;
    return mock_mapping(bytes, self_ok);
}
static HuskDualMapping husk_ios_jit_allocate_real(size_t bytes)
{
    ++trap_calls;
    return mock_mapping(bytes, trap_ok);
}
'''
suffix = r'''
static void reset(void)
{
    husk_prewarmed = (HuskDualMapping){NULL, NULL, 0};
    husk_prewarm_done = false;
    husk_prewarm_debugged = false;
    husk_prewarm_traps_tried = false;
    debugged = self_ok = trap_ok = false;
    self_calls = trap_calls = 0;
}
static void *claim(void *unused)
{
    (void)unused;
    assert(husk_ios_jit_prewarm_self(4096));
    return NULL;
}
int main(void)
{
    reset();
    debugged = self_ok = true;
    assert(husk_ios_jit_prewarm_self(4096));
    assert(self_calls == 1 && trap_calls == 0);
    assert(husk_ios_jit_prewarm_self(4096));
    assert(husk_ios_jit_prewarm(4096));
    assert(self_calls == 1 && trap_calls == 0);
    puts("PASS self-only success, reuse, no trap allocation");

    reset();
    assert(!husk_ios_jit_prewarm_self(4096));
    assert(!husk_ios_jit_prewarm_self(4096));
    assert(self_calls == 1 && trap_calls == 0);
    debugged = self_ok = true;
    assert(husk_ios_jit_prewarm_self(4096));
    assert(self_calls == 2 && trap_calls == 0);
    puts("PASS failed device check retries after CS_DEBUGGED changes");

    reset();
    assert(!husk_ios_jit_prewarm(4096));
    assert(!husk_ios_jit_prewarm(4096));
    assert(trap_calls == 1);
    debugged = self_ok = true;
    assert(husk_ios_jit_prewarm_self(4096));
    assert(self_calls == 1 && trap_calls == 1);
    puts("PASS failed legacy prewarm does not block later self-only success");

    reset();
    debugged = true;
    assert(!husk_ios_jit_prewarm_self(4096));
    assert(!husk_ios_jit_prewarm_self(4096));
    assert(self_calls == 1 && trap_calls == 0);
    trap_ok = true;
    assert(husk_ios_jit_prewarm(4096));
    assert(self_calls == 1 && trap_calls == 1);
    puts("PASS failed self-only attempt preserves later StikDebug attempt");

    reset();
    debugged = trap_ok = true;
    assert(husk_ios_jit_prewarm(4096));
    assert(husk_ios_jit_prewarm_self(4096));
    assert(trap_calls == 1 && self_calls == 0);
    puts("PASS successful debugger region reused by self-only caller");

    reset();
    debugged = true;
    assert(!husk_ios_jit_prewarm(4096));
    assert(!husk_ios_jit_prewarm_self(4096));
    assert(!husk_ios_jit_prewarm(4096));
    assert(trap_calls == 1 && self_calls == 0);
    puts("PASS repeated failures do not issue repeated trap allocations");

    reset();
    debugged = self_ok = true;
    pthread_t threads[8];
    for (int i = 0; i < 8; ++i)
        assert(pthread_create(&threads[i], NULL, claim, NULL) == 0);
    for (int i = 0; i < 8; ++i)
        assert(pthread_join(threads[i], NULL) == 0);
    assert(self_calls == 1 && trap_calls == 0);
    puts("PASS concurrent callers allocate one region");
    return 0;
}
'''
cc = shlex.split(os.environ.get("CC", "cc"))
with tempfile.TemporaryDirectory(prefix="husk-prewarm-") as temp:
    temp = Path(temp)
    harness = temp / "prewarm.c"
    harness.write_text(prefix + production + suffix)
    flags = ["-std=c11", "-Wall", "-Wextra", "-Werror", "-pthread",
             "-I", str(ROOT / "src/ios-jit")]
    subprocess.run(cc + flags + [str(harness), "-o", str(temp / "prewarm")], check=True)
    subprocess.run([str(temp / "prewarm")], check=True)
    # Also compile/link the actual non-iOS branch and the public declaration.
    smoke = temp / "smoke.c"
    smoke.write_text('#include "husk-ios-jit.h"\n'
                     'int main(void) { return husk_ios_jit_prewarm_self(4096) ? 1 : 0; }\n')
    subprocess.run(cc + flags + [str(smoke), str(ROOT / "src/ios-jit/husk-ios-jit.c"),
                                "-o", str(temp / "smoke")], check=True)
    subprocess.run([str(temp / "smoke")], check=True)
    print("PASS exported declaration and non-iOS stub compile and link")
