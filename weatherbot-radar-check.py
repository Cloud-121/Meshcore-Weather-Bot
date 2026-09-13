"""Opt-in live radar diagnostic; each request has a hard subprocess deadline."""
import asyncio
from pathlib import Path
import subprocess
import sys
import time

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))

async def check(offset):
    import radar
    original_decode = radar._decode_grib
    def timed_decode(content, compressed, latitudes, longitudes):
        start = time.monotonic()
        print(f'decode started: {len(content)} bytes, compressed={compressed}', flush=True)
        result = original_decode(content, compressed, latitudes, longitudes)
        print(f'decode completed: {time.monotonic() - start:.3f}s', flush=True)
        return result
    radar._decode_grib = timed_decode
    service = radar.RadarService('pymc-weatherbot local diagnostic')
    original_get = service._get
    async def timed_get(url, params=None):
        start = time.monotonic()
        try:
            content = await original_get(url, params)
        except Exception as exc:
            print(f'HTTP failed after {time.monotonic() - start:.3f}s: {exc}', flush=True)
            raise
        print(f'HTTP completed: {time.monotonic() - start:.3f}s, {len(content)} bytes', flush=True)
        return content
    service._get = timed_get
    start = time.monotonic()
    try:
        result = await service.snapshot(30.4515, -91.1871, offset)
        assert len(result['c']) == 812
        assert result['s'] == ('observed' if offset <= 0 else 'forecast')
        print(f"PASS: {time.monotonic() - start:.3f}s, source={result['s']}, valid={result['v']}, cells={len(result['c'])}", flush=True)
    finally:
        await service.close()

if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--child':
        asyncio.run(check(int(sys.argv[2])))
    else:
        failed = False
        for label, offset in [('now', 0), ('-1h', -1), ('+1h', 1)]:
            print(f'\n=== {label}: 60-second deadline ===', flush=True)
            try:
                result = subprocess.run([sys.executable, __file__, '--child', str(offset)], cwd=REPO, timeout=60)
                failed |= result.returncode != 0
            except subprocess.TimeoutExpired:
                print('TIMEOUT: subprocess killed after 60 seconds', flush=True)
                failed = True
        sys.exit(1 if failed else 0)
