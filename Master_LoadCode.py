"""Nạp firmware STM32 bằng STM32CubeProgrammer CLI."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
CLI_PATH_FILE = BASE_DIR / "PathProgrammerCLI.txt"


class ProgrammerError(RuntimeError):
    pass


def read_cli_path() -> Path:
    if not CLI_PATH_FILE.is_file():
        raise ProgrammerError(f"Không tìm thấy file cấu hình: {CLI_PATH_FILE}")
    # Dòng đầu tiên không trống, không phải comment là đường dẫn CLI.
    for raw_line in CLI_PATH_FILE.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip().strip('"').strip()
        if line and not line.startswith("#"):
            cli = Path(line).expanduser()
            if not cli.is_absolute():
                cli = (BASE_DIR / cli).resolve()
            if not cli.is_file():
                raise ProgrammerError(f"Không tìm thấy STM32 CLI: {cli}")
            return cli
    raise ProgrammerError(f"{CLI_PATH_FILE.name} chưa có đường dẫn CLI.")


def run_cli(cli: Path, arguments: list[str], step: str) -> str:
    command = [str(cli), *arguments]
    print(f"\n[{step}] {' '.join(command)}")
    try:
        result = subprocess.run(
            command,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=180,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout or ""
        if isinstance(output, bytes):
            output = output.decode(errors="replace")
        if output:
            print(output)
        raise ProgrammerError(f"{step} quá thời gian chờ (180 giây).") from exc
    except OSError as exc:
        raise ProgrammerError(f"Không chạy được STM32 CLI: {exc}") from exc

    output = result.stdout or ""
    if output:
        print(output.rstrip())
    if result.returncode != 0:
        raise ProgrammerError(f"{step} thất bại, CLI trả mã {result.returncode}.")
    return output


def ask_hex_file() -> Path:
    while True:
        entered = input("-> Nhập đường dẫn file firmware .hex: ").strip().strip('"')
        firmware = Path(entered).expanduser()
        if not firmware.is_absolute():
            firmware = (Path.cwd() / firmware).resolve()
        if not firmware.is_file():
            print("Không tìm thấy file. Vui lòng nhập lại.")
        elif firmware.suffix.lower() != ".hex":
            print("File cần có đuôi .hex. Vui lòng nhập lại.")
        else:
            return firmware


def ask_erase() -> bool:
    while True:
        answer = input("-> Xóa toàn bộ flash trước khi nạp? [y/N]: ").strip().lower()
        if answer in ("y", "yes", "c", "co", "có"):
            return True
        if answer in ("", "n", "no", "k", "khong", "không"):
            return False
        print("Vui lòng nhập y hoặc n.")


def ask_rdp() -> bool:
    while True:
        answer = input("-> Cài bảo mật RDP level 1? [Y/n]: ").strip().lower()
        if answer in ("", "y", "yes", "c", "co", "có"):
            return True
        if answer in ("n", "no", "k", "khong", "không"):
            return False
        print("Vui lòng nhập y hoặc n.")


def get_rdp_state(option_output: str) -> str:
    """Return level 0/1/2 from CubeProgrammer's option-byte display."""
    if re.search(r"\bRDP\s*[:=]\s*(?:0x)?BB\b", option_output, re.IGNORECASE):
        return "level 1"
    if re.search(r"\bRDP\s*:\s*Enabled\b", option_output, re.IGNORECASE):
        return "level 1"
    # STM32F1 (e.g. STM32F103) uses 0xA5 for RDP level 0; 0xAA is still protected.
    if re.search(r"\bRDP\s*[:=]\s*(?:0x)?A5\b", option_output, re.IGNORECASE):
        return "level 0"
    if re.search(r"\bRDP\s*:\s*Disabled\b", option_output, re.IGNORECASE):
        return "level 0"
    if re.search(r"\bRDP\s*[:=]\s*(?:0x)?CC\b", option_output, re.IGNORECASE):
        return "level 2"
    return "unknown"


def program_once(cli: Path, firmware: Path, erase: bool, enable_rdp: bool) -> None:
    # Mỗi lệnh CLI mở kết nối và tự đóng khi tiến trình kết thúc.
    run_cli(cli, ["-c", "port=SWD", "mode=UR"], "Kết nối chip")
    # CubeProgrammer phải xóa các sector firmware khi download, kể cả khi
    # người dùng không chọn mass erase toàn chip. RDP1 phải được hạ trước đó.
    option_output = run_cli(
        cli,
        ["-c", "port=SWD", "mode=UR", "-ob", "displ"],
        "Kiểm tra RDP trước khi nạp/xóa flash",
    )
    rdp_state = get_rdp_state(option_output)
    if rdp_state == "level 1":
        print("RDP level 1 đang bật; chuyển về level 0 trước khi nạp.")
        level0_output = run_cli(
            cli,
            ["-c", "port=SWD", "mode=UR", "-ob", "RDP=0xA5", "-ob", "displ"],
            "Hạ RDP về level 0 (STM32 sẽ tự xóa flash khi hạ bảo vệ)",
        )
        if get_rdp_state(level0_output) != "level 0":
            raise ProgrammerError("Không xác nhận được RDP level 0; dừng trước khi nạp.")
    elif rdp_state != "level 0":
        raise ProgrammerError(
            f"Không xác định được trạng thái RDP an toàn ({rdp_state}); "
            "dừng trước khi nạp/xóa flash. Nếu chip đang ở RDP level 2 thì không thể hạ bằng lệnh này."
        )

    if erase:
        run_cli(cli, ["-c", "port=SWD", "mode=UR", "-e", "all"], "Xóa flash")
    run_cli(
        cli,
        ["-c", "port=SWD", "mode=UR", "-d", str(firmware), "-v"],
        "Nạp và verify firmware",
    )
    if enable_rdp:
        option_output = run_cli(
            cli,
            [
                "-c", "port=SWD", "mode=UR",
                "-ob", "RDP=0xBB",
                "-ob", "displ",
            ],
            "Đặt và xác nhận RDP level 1",
        )
        if get_rdp_state(option_output) != "level 1":
            raise ProgrammerError(
                "Không xác nhận được RDP level 1 (RDP=0xBB hoặc RDP: Enabled) "
                "trong kết quả đọc option bytes."
            )
    run_cli(
        cli,
        ["-c", "port=SWD", "mode=HOTPLUG", "-rst"],
        "Kết nối lại thiết bị và reset để chạy firmware",
    )
    print("\n[Ngắt kết nối] STM32CubeProgrammer CLI đã kết thúc phiên kết nối.")


def main() -> int:
    try:
        cli = read_cli_path()
        print(f"Đã đọc CLI: {cli}")
        firmware = ask_hex_file()
        erase = ask_erase()
        enable_rdp = ask_rdp()
    except (ProgrammerError, OSError) as exc:
        print(f"Lỗi cấu hình/đầu vào: {exc}", file=sys.stderr)
        return 1

    print("\nSẵn sàng. Nhấn Enter để bắt đầu mỗi lần nạp; nhập q rồi Enter để thoát.")
    while True:
        try:
            print("\n-------------New section---------------------------------------")
            command = input("-> Enter = nạp | q = thoát: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nĐã dừng chương trình.")
            return 0
        if command in ("q", "quit", "exit"):
            print("Đã thoát.")
            return 0
        try:
            program_once(cli, firmware, erase, enable_rdp)
            print("\nNẠP THÀNH CÔNG.")
        except ProgrammerError as exc:
            print(f"\nLỖI: {exc}")
            print("Kiểm tra kết nối/cấu hình rồi nhấn Enter để thử lại.")


if __name__ == "__main__":
    raise SystemExit(main())
