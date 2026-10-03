#!/usr/bin/env bash
set -euo pipefail

usage() {
    cat >&2 <<'EOF'
Usage: create-htg940-fpga-bundle.sh [options]

Package existing FPGA test build outputs into a verified ZIP archive.

Options:
  --caliptra-rom PATH  Caliptra FPGA ROM binary
  --mcu-rom PATH       MCU FPGA recovery ROM binary
  --recovery-zip PATH  ZIP containing caliptra_fw.bin, soc_manifest.bin,
                       and mcu_runtime.bin
  --output PATH        Output bundle ZIP
  -h, --help           Show this help
EOF
}

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
mcu_repo="$(cd -- "$script_dir/../.." && pwd)"
caliptra_repo="$mcu_repo/caliptra-sw"

caliptra_rom="$caliptra_repo/target/riscv32imc-unknown-none-elf/firmware/caliptra-fpga-rom.bin"
mcu_rom="$mcu_repo/target/riscv32imc-unknown-none-elf/release/caliptra-mcu-rom-fpga-test-lpcip-usb-ocp-recovery.bin"
recovery_zip="$mcu_repo/target/usb-recovery-minimal.zip"
output="$mcu_repo/target/htg940-fpga-test-bundle.zip"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --caliptra-rom)
            [[ $# -ge 2 ]] || { usage; exit 2; }
            caliptra_rom="$2"
            shift 2
            ;;
        --mcu-rom)
            [[ $# -ge 2 ]] || { usage; exit 2; }
            mcu_rom="$2"
            shift 2
            ;;
        --recovery-zip)
            [[ $# -ge 2 ]] || { usage; exit 2; }
            recovery_zip="$2"
            shift 2
            ;;
        --output)
            [[ $# -ge 2 ]] || { usage; exit 2; }
            output="$2"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "ERROR: Unknown argument: $1" >&2
            usage
            exit 2
            ;;
    esac
done

for command in cp git mktemp sha384sum unzip zip; do
    command -v "$command" >/dev/null 2>&1 || {
        echo "ERROR: Required command is not installed: $command" >&2
        exit 2
    }
done

require_file() {
    local path="$1"
    local description="$2"
    [[ -f "$path" ]] || {
        echo "ERROR: $description does not exist: $path" >&2
        exit 2
    }
}

require_file "$caliptra_rom" "Caliptra ROM"
require_file "$mcu_rom" "MCU ROM"
require_file "$recovery_zip" "Recovery artifact ZIP"

output="$(realpath -m -- "$output")"
recovery_zip="$(realpath -- "$recovery_zip")"
if [[ "$output" == "$recovery_zip" ]]; then
    echo "ERROR: Output bundle must not overwrite the recovery artifact ZIP." >&2
    exit 2
fi
mkdir -p -- "$(dirname -- "$output")"

stage_dir="$(mktemp -d)"
cleanup() {
    rm -rf -- "$stage_dir"
}
trap cleanup EXIT

cp -- "$caliptra_rom" "$stage_dir/caliptra_rom.bin"
cp -- "$mcu_rom" "$stage_dir/mcu_rom.bin"
unzip -q "$recovery_zip" \
    caliptra_fw.bin soc_manifest.bin mcu_runtime.bin \
    -d "$stage_dir"

artifacts=(
    caliptra_rom.bin
    mcu_rom.bin
    caliptra_fw.bin
    soc_manifest.bin
    mcu_runtime.bin
)

for artifact in "${artifacts[@]}"; do
    require_file "$stage_dir/$artifact" "Bundle artifact $artifact"
done

(
    cd "$stage_dir"
    sha384sum "${artifacts[@]}" > SHA384SUMS
)

caliptra_commit="$(git -C "$caliptra_repo" rev-parse HEAD 2>/dev/null || printf 'unknown')"
mcu_commit="$(git -C "$mcu_repo" rev-parse HEAD 2>/dev/null || printf 'unknown')"
cat >"$stage_dir/BUNDLE_INFO.txt" <<EOF
format_version=1
created_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)
mcu_commit=$mcu_commit
caliptra_commit=$caliptra_commit
EOF

rm -f -- "$output"
(
    cd "$stage_dir"
    zip -q "$output" "${artifacts[@]}" SHA384SUMS BUNDLE_INFO.txt
)

echo "Created FPGA test bundle: $output"
unzip -l "$output"