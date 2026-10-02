#!/usr/bin/env python3
"""Patch HTG940 ROM initialization using updatemem, never synthesize or route.

Inputs: matching base BIT/MMI from ONE implementation; Core ROM <=96 KiB,
MCU ROM <=128 KiB. .bin is raw bytes at ROM offset zero; .mem is 64-bit
hex words in XPM order (optional @ hexadecimal WORD addresses). Short
images are zero padded. ELF is intentionally rejected: use the project's
ROM build/conversion flow to obtain the final ROM binary including its digest.

First use --dry-run and validate with the original ROM images. This utility
cannot prove that a BIT and MMI belong together; supply the matching pair.
OTP and firmware-flash storage are not modified. Requires Python 3.8+ and
Vivado updatemem on PATH. No third-party Python packages required.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

ROMS = {'core': ('imem_inst1', 96 * 1024), 'mcu': ('mcu_rom', 128 * 1024)}


def sha(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def number(value):
    return int(value, 16 if value.lower().startswith('0x') else 10)


def mapping(mmi, instance, size):
    root = ET.parse(mmi).getroot()
    for el in root.iter():
        el.tag = el.tag.rsplit('}', 1)[-1]
    matches = [el for el in root.iter('MemoryArray')
               if instance in el.get('InstPath', '').split('/')]
    if len(matches) != 1:
        raise ValueError('Expected exactly one MemoryArray for %s; found %d' % (instance, len(matches)))
    mem = matches[0]
    if mem.get('MemoryPrimitive') != 'block':
        raise ValueError(instance + ': MMI memory is not block RAM')
    layouts = mem.findall('MemoryLayout')
    if len(layouts) != 1 or number(layouts[0].get('CoreMemory_Width', '0')) != 64:
        raise ValueError(instance + ': expected one 64-bit MemoryLayout')
    # Validate every logical data bit covers the complete ROM address range.
    lanes = [[] for _ in range(64)]
    for bram in layouts[0].findall('BRAM'):
        parity = bram.find('Parity')
        if parity is not None and parity.get('ON', '').lower() == 'true':
            raise ValueError(instance + ': parity/ECC mapping requires review')
        dw, ar = bram.find('DataWidth_PortA'), bram.find('AddressRange_PortA')
        if dw is None or ar is None:
            raise ValueError(instance + ': missing PortA mapping')
        lo, hi = number(dw.attrib['LSB']), number(dw.attrib['MSB'])
        begin, end = number(ar.attrib['Begin']), number(ar.attrib['End'])
        if not (0 <= lo <= hi < 64 and 0 <= begin <= end < size // 8):
            raise ValueError(instance + ': unexpected bit/address range; inspect MMI')
        for lane in range(lo, hi + 1):
            lanes[lane].append((begin, end))
    for intervals in lanes:
        cursor = 0
        for begin, end in sorted(intervals):
            if begin != cursor:
                raise ValueError(instance + ': missing/overlapping address coverage; inspect MMI')
            cursor = end + 1
        if cursor != size // 8:
            raise ValueError(instance + ': MMI size does not match configured ROM capacity')
    return mem.attrib['InstPath']


def image_bytes(path, size):
    raw = path.read_bytes()
    if raw.startswith(b'\x7fELF'):
        raise ValueError('ELF not accepted directly; supply the final project ROM .bin or .mem')
    if path.suffix.lower() == '.bin':
        if not 0 < len(raw) <= size:
            raise ValueError('%s: binary is empty or exceeds %d bytes' % (path, size))
        return raw.ljust(size, b'\0')
    if path.suffix.lower() != '.mem':
        raise ValueError('Supported image extensions: .bin and .mem')
    text = re.sub(r'/\*.*?\*/', '', raw.decode('ascii'), flags=re.S)
    text = re.sub(r'//[^\n]*', '', text)
    output, used, addr = bytearray(size), set(), 0
    for token in text.split():
        if token.startswith('@'):
            addr = int(token[1:], 16)
            if not 0 <= addr < size // 8:
                raise ValueError('MEM address outside ROM')
            continue
        if not re.fullmatch(r'[0-9a-fA-F]{1,16}', token):
            raise ValueError('Invalid 64-bit MEM word: ' + token)
        if addr in used or not 0 <= addr < size // 8:
            raise ValueError('Duplicate or out-of-range MEM word address')
        output[addr*8:addr*8+8] = int(token, 16).to_bytes(8, 'little')
        used.add(addr)
        addr += 1
    if not used:
        raise ValueError('Empty MEM image')
    return bytes(output)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--bit', required=True, type=Path)
    p.add_argument('--mmi', required=True, type=Path)
    p.add_argument('--core-rom', type=Path)
    p.add_argument('--mcu-rom', type=Path)
    p.add_argument('--out', required=True, type=Path)
    p.add_argument('--updatemem', default='updatemem')
    p.add_argument('--dry-run', action='store_true', help='validate only; do not invoke updatemem or create output')
    a = p.parse_args()
    if not (a.core_rom or a.mcu_rom):
        p.error('Supply --core-rom and/or --mcu-rom')
    bit, mmi, out = a.bit.resolve(), a.mmi.resolve(), a.out.resolve()
    report = Path(str(out) + '.json')
    if not bit.is_file() or not mmi.is_file() or bit.stat().st_size == 0:
        raise ValueError('Missing/empty base bitstream or missing MMI')
    if out.exists() or report.exists() or out in (bit, mmi):
        raise ValueError('Output or report already exists, or output aliases an input; choose a new filename')
    jobs = []
    for role, (instance, size) in ROMS.items():
        src = getattr(a, role + '_rom')
        if src:
            src = src.resolve()
            path = mapping(mmi, instance, size)
            data = image_bytes(src, size)
            jobs.append((role, src, path, data))
            print('%s: %s -> %s (%d bytes padded)' % (role, src, path, len(data)))
    record = {'base_bit': str(bit), 'base_sha256': sha(bit), 'mmi': str(mmi),
              'mmi_sha256': sha(mmi), 'roms': [
                  {'role': role, 'input': str(src), 'input_sha256': sha(src),
                   'memory_path': path, 'padded_sha256': hashlib.sha256(data).hexdigest()}
                  for role, src, path, data in jobs]}
    if a.dry_run:
        print('INPUT_VALIDATION_PASS: no tool invoked; BIT/MMI pairing and hardware behavior remain unverified.')
        return
    tool = shutil.which(a.updatemem)
    if not tool:
        raise ValueError('updatemem not found; source the Vivado environment')
    out.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive output creation preserves any pre-existing file, even on a race.
    with tempfile.TemporaryDirectory(prefix='htg940_rom_', dir=out.parent) as tmp:
        work = Path(tmp)
        current = bit
        for role, src, path, data in jobs:
            mem = work / (role + '.mem')
            mem.write_text('@0\n' + ''.join('%016X\n' % int.from_bytes(data[i:i+8], 'little')
                                          for i in range(0, len(data), 8)))
            nxt = work / (role + '.bit')
            cmd = [tool, '-meminfo', str(mmi), '-data', str(mem), '-proc', path,
                   '-bit', str(current), '-out', str(nxt)]
            print('Running:', ' '.join(cmd), flush=True)
            subprocess.run(cmd, cwd=work, check=True)
            if not nxt.is_file() or not nxt.stat().st_size:
                raise RuntimeError('updatemem returned without producing a nonempty bitstream')
            current = nxt
        if sha(bit) != record['base_sha256'] or sha(mmi) != record['mmi_sha256']:
            raise RuntimeError('Base BIT or MMI changed during update; output not published')
        record.update(output=str(out), output_sha256=sha(current))
        with out.open('xb') as dest, current.open('rb') as source:
            shutil.copyfileobj(source, dest)
        with report.open('x') as f:
            json.dump(record, f, indent=2)
            f.write('\n')
    print('BITSTREAM_UPDATED:', out)
    print('RECORD:', report)
    print('Not a boot PASS: program and verify UART behavior separately.')


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError, ET.ParseError, subprocess.CalledProcessError) as exc:
        print('UPDATE_FAILED:', exc, file=sys.stderr)
        sys.exit(1)
