"""Wrapper around KiCad 10.0.8 CLI (`kicad-cli.exe`) for exports, zone refilling, DRC/ERC, and validation."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

try:
    from pcb_inspector import check_kicad_lock_files
except ImportError:
    from .pcb_inspector import check_kicad_lock_files

DEFAULT_KICAD_CLI_PATHS: tuple[Path, ...] = ()

DEFAULT_SVG_LAYERS: tuple[str, ...] = (
    "F.Cu",
    "B.Cu",
    "F.Silkscreen",
    "B.Silkscreen",
    "Edge.Cuts",
)


class KiCadCLIError(RuntimeError):
    """Raised when `kicad-cli` exits with a non-zero status code."""

    def __init__(
        self,
        message: str,
        *,
        command: list[str],
        returncode: int,
        stdout: str = "",
        stderr: str = "",
    ) -> None:
        """Initialize the CLI error with subprocess command details and output streams.

        Args:
            message: Human-readable failure summary.
            command: Full command-line token list executed.
            returncode: Non-zero process exit code.
            stdout: Captured standard output text.
            stderr: Captured standard error text.

        Returns:
            None.
        """
        super().__init__(message)
        self.command = command
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _iter_default_kicad_cli_candidates() -> list[Path]:
    """Build prioritized candidate paths for `kicad-cli` across Windows, macOS, and Linux.

    Returns:
        Ordered list of candidate `Path` objects with newest KiCad versions first.
    """
    found: list[Path] = list(DEFAULT_KICAD_CLI_PATHS)
    win_roots: list[Path] = []
    for env_key in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        val = os.environ.get(env_key)
        if val:
            base = Path(val)
            win_roots.append(base / "KiCad")
            win_roots.append(base / "Programs" / "KiCad")
    win_roots.extend(
        [
            Path(r"C:\Program Files\KiCad"),
            Path(r"C:\Program Files (x86)\KiCad"),
            Path.home() / "AppData" / "Local" / "Programs" / "KiCad",
        ]
    )
    seen_dirs: set[Path] = set()
    for kicad_root in win_roots:
        if kicad_root in seen_dirs or not kicad_root.is_dir():
            continue
        seen_dirs.add(kicad_root)
        try:
            ver_dirs = sorted(
                (d for d in kicad_root.iterdir() if d.is_dir()),
                key=lambda p: p.name,
                reverse=True,
            )
        except OSError:
            ver_dirs = []
        for ver_dir in ver_dirs:
            found.append(ver_dir / "bin" / "kicad-cli.exe")
        found.append(kicad_root / "bin" / "kicad-cli.exe")

    found.extend(
        [
            Path.home() / "scoop" / "apps" / "kicad" / "current" / "bin" / "kicad-cli.exe",
            Path("/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"),
            Path.home() / "Applications" / "KiCad" / "KiCad.app" / "Contents" / "MacOS" / "kicad-cli",
            Path("/usr/bin/kicad-cli"),
            Path("/usr/local/bin/kicad-cli"),
            Path("/snap/bin/kicad-cli"),
        ]
    )
    return found


def discover_kicad_cli(explicit_path: str | Path | None = None) -> Path:
    """Locate `kicad-cli.exe` via explicit path, environment variable, default installs, or PATH.

    Args:
        explicit_path: Optional explicit path to `kicad-cli.exe`.

    Returns:
        Resolved `Path` to the `kicad-cli` executable.
    """
    candidates: list[Path] = []
    if explicit_path is not None:
        candidates.append(Path(explicit_path))

    env_path = os.environ.get("KICAD_CLI_PATH")
    if env_path:
        candidates.append(Path(env_path))

    candidates.extend(_iter_default_kicad_cli_candidates())

    for candidate in candidates:
        if candidate.is_file():
            return candidate

    for binary_name in ("kicad-cli", "kicad-cli.exe"):
        found = shutil.which(binary_name)
        if found:
            return Path(found)

    raise FileNotFoundError(
        "Could not locate kicad-cli.exe (checked KICAD_CLI_PATH, standard KiCad install directories, and PATH). "
        "Set KICAD_CLI_PATH or install KiCad 10.0+. "
        "Offline Python tools (create_kicad_project, create_schematic, create_netlist_from_bom, "
        "add_footprints_to_pcb, edit_pcb_layout, autoroute_pcb_nets, inspect_pcb_layout, "
        "find_unrouted_pads, analyze_pcb_traces, check_mechanical_fit) remain available without kicad-cli."
    )


def _require_existing_path(path: str | Path, label: str = "Input path") -> Path:
    """Validate that a filesystem path exists and return its resolved `Path`.

    Args:
        path: Input file or directory path.
        label: Descriptive label used in error messages if missing.

    Returns:
        Resolved `Path` guaranteed to exist.
    """
    resolved = Path(path).expanduser().resolve()
    if not resolved.exists():
        raise FileNotFoundError(f"{label} does not exist: {resolved}")
    return resolved


@contextmanager
def _optional_temp_dir(target_dir: str | Path | None) -> Iterator[Path]:
    """Yield a guaranteed directory from `target_dir`, or a temporary directory if None.

    Args:
        target_dir: Existing or desired directory path, or `None` for a temporary directory.

    Returns:
        Context manager yielding the working `Path`.
    """
    if target_dir is not None:
        out_dir = Path(target_dir).expanduser().resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        yield out_dir
    else:
        with tempfile.TemporaryDirectory(prefix="kicad_mcp_") as tmp_dir:
            yield Path(tmp_dir)


def _count_severities(items: list[dict[str, Any]]) -> dict[str, int]:
    """Count violation entries grouped by lowercase severity level.

    Args:
        items: List of violation dictionaries from a KiCad DRC/ERC report.

    Returns:
        Mapping of severity name to item count.
    """
    counts = Counter(str(item.get("severity", "unknown")).lower() for item in items)
    return dict(counts)


def _attach_lock_info(
    result: dict[str, Any],
    target_path: str | Path,
    lock_info: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Attach KiCad `.lck` file metadata and lock warning (when locked) onto a result dict.

    Args:
        result: Result dictionary produced by a KiCad CLI operation.
        target_path: Path to the target KiCad file or directory inspected for locks.
        lock_info: Optional pre-computed output from `check_kicad_lock_files`.

    Returns:
        The updated `result` dictionary with `"lock_files"` and optional `"lock_warning"`.
    """
    info = lock_info if lock_info is not None else check_kicad_lock_files(str(target_path))
    lock_files = info.get("lock_files", [])
    result["lock_files"] = lock_files
    if info.get("locked"):
        result["lock_warning"] = info.get("warning") or (
            f"KiCad GUI lock detected ({len(lock_files)} .lck file(s))."
        )
    return result


def _finalize_pcb_export_result(
    result: dict[str, Any],
    board: Path,
    target_board: Path,
    refill_zones: bool,
) -> dict[str, Any]:
    """Attach zone-refill mode metadata and KiCad lock status to a PCB export result.

    Args:
        result: Export metadata dictionary produced by `_export_to_file` or `_export_to_dir`.
        board: Original `.kicad_pcb` path requested by the caller.
        target_board: Actual board path passed to `kicad-cli` (original or staged copy).
        refill_zones: Whether zone refilling was requested prior to export.

    Returns:
        Updated export dictionary with `"zones_refilled"`, `"zones_refill_mode"`, and lock info.
    """
    result["zones_refilled"] = refill_zones
    result["zones_refill_mode"] = (
        "disabled"
        if not refill_zones
        else ("in_place" if target_board == board else "staged_copy")
    )
    return _attach_lock_info(result, board)


class KiCadCLI:
    """High-level interface to KiCad 10.0.8 CLI subcommands."""

    def __init__(
        self,
        cli_path: str | Path | None = None,
        timeout: float = 240.0,
    ) -> None:
        """Initialize the CLI wrapper by discovering `kicad-cli.exe` and setting timeout.

        Args:
            cli_path: Optional explicit path to `kicad-cli.exe`.
            timeout: Default subprocess timeout in seconds.

        Returns:
            None.
        """
        self.cli_path = discover_kicad_cli(cli_path)
        self.timeout = timeout

    @contextmanager
    def _isolated_env_for_args(self, args: list[str]) -> Iterator[dict[str, str] | None]:
        """Build an isolated `KICAD_CONFIG_HOME` with only referenced footprint/symbol libraries for fast DRC/ERC.

        Args:
            args: `kicad-cli` argument list being executed.

        Returns:
            Context manager yielding an environment dictionary or `None` when isolation is unnecessary.
        """
        if len(args) < 2 or args[:2] not in (["pcb", "drc"], ["sch", "erc"]):
            yield None
            return

        target_file = Path(args[-1])
        if not target_file.is_file():
            yield None
            return

        install_root = self.cli_path.parent.parent / "share" / "kicad"
        fp_root = install_root / "footprints"
        sym_root = install_root / "symbols"
        if not fp_root.is_dir() and not sym_root.is_dir():
            yield None
            return

        proj_dir = target_file.parent
        fp_libs: set[str] = set()
        sym_libs: set[str] = set()
        for candidate in (proj_dir / f"{target_file.stem}.kicad_pcb", proj_dir / f"{target_file.stem}.kicad_sch"):
            if candidate.is_file():
                raw = candidate.read_text(encoding="utf-8", errors="replace")
                for m_fp in re.finditer(r'\(footprint\s+"([^":]+):', raw):
                    fp_libs.add(m_fp.group(1))
                for m_sym in re.finditer(r'\(lib_id\s+"([^":]+):', raw):
                    sym_libs.add(m_sym.group(1))

        with tempfile.TemporaryDirectory(prefix="kicad_cfg_") as tmp_dir:
            td = Path(tmp_dir)
            cfg_ver = td / "cfg" / "10.0"
            cfg_ver.mkdir(parents=True, exist_ok=True)
            (td / "appdata").mkdir(exist_ok=True)
            (td / "localappdata").mkdir(exist_ok=True)

            fp_lines: list[str] = []
            for lib_name in sorted(fp_libs):
                pretty = fp_root / f"{lib_name}.pretty"
                if pretty.is_dir():
                    uri = str(pretty).replace("\\", "/")
                    fp_lines.append(f'  (lib (name "{lib_name}")(type "KiCad")(uri "{uri}")(options "")(descr ""))')
            (cfg_ver / "fp-lib-table").write_text(
                "(fp_lib_table\n  (version 7)\n" + "\n".join(fp_lines) + "\n)\n",
                encoding="utf-8",
            )

            sym_lines: list[str] = []
            for lib_name in sorted(sym_libs):
                sym_file = sym_root / f"{lib_name}.kicad_sym"
                if sym_file.is_file():
                    uri = str(sym_file).replace("\\", "/")
                    sym_lines.append(f'  (lib (name "{lib_name}")(type "KiCad")(uri "{uri}")(options "")(descr ""))')
            (cfg_ver / "sym-lib-table").write_text(
                "(sym_lib_table\n  (version 7)\n" + "\n".join(sym_lines) + "\n)\n",
                encoding="utf-8",
            )

            env = os.environ.copy()
            env["KICAD_CONFIG_HOME"] = str(td / "cfg")
            env["APPDATA"] = str(td / "appdata")
            env["LOCALAPPDATA"] = str(td / "localappdata")
            yield env

    def _run(
        self,
        args: list[str],
        *,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Execute `kicad-cli` with the given arguments and raise `KiCadCLIError` on failure.

        Args:
            args: Subcommand and option tokens passed after `kicad-cli`.
            timeout: Optional override timeout in seconds.

        Returns:
            Completed `subprocess.CompletedProcess` with captured text streams.
        """
        cmd = [str(self.cli_path), *args]
        with self._isolated_env_for_args(args) as env:
            proc = subprocess.run(
                cmd,
                env=env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout or self.timeout,
                check=False,
            )
        if proc.returncode != 0:
            detail = proc.stderr.strip() or proc.stdout.strip() or f"exit code {proc.returncode}"
            raise KiCadCLIError(
                f"kicad-cli failed ({proc.returncode}): {detail}",
                command=cmd,
                returncode=proc.returncode,
                stdout=proc.stdout,
                stderr=proc.stderr,
            )
        return proc

    def _refill_board_file(self, board_file: Path) -> subprocess.CompletedProcess[str]:
        """Run `kicad-cli pcb drc --refill-zones --save-board` on the given board file.

        Args:
            board_file: Path to the `.kicad_pcb` file to refill and save in-place.

        Returns:
            Completed `subprocess.CompletedProcess` from the refill command.
        """
        with tempfile.TemporaryDirectory(prefix="kicad_refill_") as tmp_dir:
            report_file = Path(tmp_dir) / "refill_report.json"
            return self._run(
                [
                    "pcb",
                    "drc",
                    "--refill-zones",
                    "--save-board",
                    "--format",
                    "json",
                    "--output",
                    str(report_file),
                    str(board_file),
                ]
            )

    @contextmanager
    def _prepared_pcb_for_export(
        self,
        board: Path,
        refill_zones: bool = True,
    ) -> Iterator[Path]:
        """Yield a `.kicad_pcb` path ready for export, refilling zones safely around GUI locks.

        Args:
            board: Resolved path to the source `.kicad_pcb` file.
            refill_zones: Whether to refill copper zones before yielding the board path.

        Returns:
            Context manager yielding either `board` or a zone-refilled staged temporary copy.
        """
        if not refill_zones:
            yield board
            return

        lock_info = check_kicad_lock_files(str(board))
        if not lock_info.get("target_locked"):
            self._refill_board_file(board)
            yield board
            return

        with tempfile.TemporaryDirectory(prefix="kicad_stage_") as tmp_dir:
            stage_dir = Path(tmp_dir)
            staged_board = stage_dir / board.name
            shutil.copy2(board, staged_board)
            for ext in (".kicad_pro", ".kicad_sch", ".kicad_prl"):
                side_file = board.with_suffix(ext)
                if side_file.is_file():
                    shutil.copy2(side_file, stage_dir / side_file.name)
            for tbl_name in ("fp-lib-table", "sym-lib-table"):
                tbl_file = board.parent / tbl_name
                if tbl_file.is_file():
                    shutil.copy2(tbl_file, stage_dir / tbl_name)
            libs_dir = board.parent / "libs"
            if libs_dir.is_dir():
                shutil.copytree(libs_dir, stage_dir / "libs")
            self._refill_board_file(staged_board)
            yield staged_board

    def _export_to_file(
        self,
        subcommand: list[str],
        input_path: Path,
        output_path: str | Path,
        extra_args: list[str] | None = None,
    ) -> dict[str, Any]:
        """Run a file-producing CLI export and return output metadata.

        Args:
            subcommand: KiCad CLI subcommand token list.
            input_path: Input KiCad file path.
            output_path: Destination output file path.
            extra_args: Optional additional CLI flags placed before `input_path`.

        Returns:
            Dictionary containing export status, output path, size, command, and logs.
        """
        out_file = Path(output_path).expanduser().resolve()
        out_file.parent.mkdir(parents=True, exist_ok=True)
        args = [*subcommand, "--output", str(out_file), *(extra_args or []), str(input_path)]
        proc = self._run(args)
        return {
            "status": "ok",
            "output_path": str(out_file),
            "size_bytes": out_file.stat().st_size if out_file.is_file() else 0,
            "command": [str(self.cli_path), *args],
            "stdout": proc.stdout.strip(),
            "stderr": proc.stderr.strip(),
        }

    def _export_to_dir(
        self,
        subcommand: list[str],
        input_path: Path,
        output_dir: str | Path,
        extra_args: list[str] | None = None,
    ) -> dict[str, Any]:
        """Run a directory-producing CLI export and list generated files.

        Args:
            subcommand: KiCad CLI subcommand token list.
            input_path: Input KiCad file path.
            output_dir: Destination output directory path.
            extra_args: Optional additional CLI flags placed before `input_path`.

        Returns:
            Dictionary containing export status, output directory, generated files, and logs.
        """
        out_dir = Path(output_dir).expanduser().resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        before = {p.name for p in out_dir.iterdir() if p.is_file()}
        args = [*subcommand, "--output", str(out_dir), *(extra_args or []), str(input_path)]
        proc = self._run(args)
        after = sorted(str(p) for p in out_dir.iterdir() if p.is_file())
        new_files = sorted(str(p) for p in out_dir.iterdir() if p.is_file() and p.name not in before)
        return {
            "status": "ok",
            "output_dir": str(out_dir),
            "generated_files": new_files or after,
            "command": [str(self.cli_path), *args],
            "stdout": proc.stdout.strip(),
            "stderr": proc.stderr.strip(),
        }

    def get_version(self) -> str:
        """Return the installed KiCad CLI version string.

        Returns:
            Plain version string reported by `kicad-cli version`.
        """
        proc = self._run(["version", "--format", "plain"])
        return proc.stdout.strip()

    def refill_pcb_zones(
        self,
        pcb_path: str,
        output_path: str | None = None,
        force_when_locked: bool = False,
    ) -> dict[str, Any]:
        """Refill copper zones on a `.kicad_pcb` board in-place, or to a staged output file when locked.

        Args:
            pcb_path: Path to the target `.kicad_pcb` file.
            output_path: Optional destination `.kicad_pcb` path (defaults to `<stem>_refilled.kicad_pcb` when locked).
            force_when_locked: Whether to allow in-place overwrite even when a `.lck` file exists.

        Returns:
            Dictionary with refill status (`"ok"` or `"ok_staged"`), `refilled_in_place`, `output_path`, and lock info.
        """
        board = _require_existing_path(pcb_path, "PCB file")
        lock_info = check_kicad_lock_files(str(board))
        if (lock_info.get("target_locked") and not force_when_locked) or output_path is not None:
            dest = (
                Path(output_path).expanduser().resolve()
                if output_path is not None
                else board.with_name(f"{board.stem}_refilled{board.suffix}")
            )
            dest.parent.mkdir(parents=True, exist_ok=True)
            with self._prepared_pcb_for_export(board, refill_zones=True) as staged_board:
                if staged_board != dest:
                    shutil.copy2(staged_board, dest)
            return _attach_lock_info(
                {
                    "status": "ok" if dest == board else "ok_staged",
                    "refilled_in_place": dest == board,
                    "pcb_path": str(board),
                    "output_path": str(dest),
                },
                board,
                lock_info=lock_info,
            )

        proc = self._refill_board_file(board)
        return _attach_lock_info(
            {
                "status": "ok",
                "refilled_in_place": True,
                "pcb_path": str(board),
                "output_path": str(board),
                "forced": bool(force_when_locked and lock_info.get("target_locked")),
                "stdout": proc.stdout.strip(),
                "stderr": proc.stderr.strip(),
            },
            board,
            lock_info=lock_info,
        )

    def export_gerbers(
        self,
        pcb_path: str,
        output_dir: str,
        layers: list[str] | None = None,
        refill_zones: bool = True,
    ) -> dict[str, Any]:
        """Export Gerber files and job file (`.gbrjob`) for a `.kicad_pcb` board.

        Args:
            pcb_path: Path to the `.kicad_pcb` board file.
            output_dir: Directory where Gerber files will be written.
            layers: Optional list of KiCad layer names to export.
            refill_zones: Whether to refill copper zones (safely staged if locked) before export.

        Returns:
            Dictionary with generated file paths, zone-refill metadata, and lock info.
        """
        board = _require_existing_path(pcb_path, "PCB file")
        extra = ["--layers", ",".join(layers)] if layers else []
        with self._prepared_pcb_for_export(board, refill_zones=refill_zones) as target_board:
            result = self._export_to_dir(["pcb", "export", "gerbers"], target_board, output_dir, extra)
        return _finalize_pcb_export_result(result, board, target_board, refill_zones)

    def export_drill(self, pcb_path: str, output_dir: str) -> dict[str, Any]:
        """Export Excellon drill files and PDF drill map for a `.kicad_pcb` board.

        Args:
            pcb_path: Path to the `.kicad_pcb` board file.
            output_dir: Directory where drill files and PDF map will be written.

        Returns:
            Dictionary with generated file paths and KiCad lock info.
        """
        board = _require_existing_path(pcb_path, "PCB file")
        extra = [
            "--format",
            "excellon",
            "--excellon-units",
            "mm",
            "--generate-map",
            "--map-format",
            "pdf",
        ]
        result = self._export_to_dir(["pcb", "export", "drill"], board, output_dir, extra)
        return _attach_lock_info(result, board)

    def export_step(
        self,
        pcb_path: str,
        output_path: str,
        refill_zones: bool = True,
    ) -> dict[str, Any]:
        """Export a 3D STEP model from a `.kicad_pcb` board.

        Args:
            pcb_path: Path to the `.kicad_pcb` board file.
            output_path: Destination `.step` file path.
            refill_zones: Whether to refill copper zones (safely staged if locked) before export.

        Returns:
            Dictionary with exported STEP metadata, zone-refill metadata, and lock info.
        """
        board = _require_existing_path(pcb_path, "PCB file")
        extra = ["--force", "--subst-models"]
        with self._prepared_pcb_for_export(board, refill_zones=refill_zones) as target_board:
            result = self._export_to_file(["pcb", "export", "step"], target_board, output_path, extra)
        return _finalize_pcb_export_result(result, board, target_board, refill_zones)

    def export_sch_pdf(self, sch_path: str, output_path: str) -> dict[str, Any]:
        """Export a `.kicad_sch` schematic (including hierarchical sheets) to PDF.

        Args:
            sch_path: Path to the root `.kicad_sch` file.
            output_path: Destination `.pdf` file path.

        Returns:
            Dictionary with exported PDF metadata and KiCad lock info.
        """
        schematic = _require_existing_path(sch_path, "Schematic file")
        result = self._export_to_file(["sch", "export", "pdf"], schematic, output_path)
        return _attach_lock_info(result, schematic)

    def export_pcb_svg(
        self,
        pcb_path: str,
        output_path: str,
        layers: list[str] | None = None,
        refill_zones: bool = True,
    ) -> dict[str, Any]:
        """Export a single combined SVG image of the requested PCB layers.

        Args:
            pcb_path: Path to the `.kicad_pcb` board file.
            output_path: Destination `.svg` file path.
            layers: Optional list of layer names; defaults to `DEFAULT_SVG_LAYERS`.
            refill_zones: Whether to refill copper zones (safely staged if locked) before export.

        Returns:
            Dictionary with exported SVG metadata, zone-refill metadata, and lock info.
        """
        board = _require_existing_path(pcb_path, "PCB file")
        selected_layers = layers if layers else list(DEFAULT_SVG_LAYERS)
        extra = ["--mode-single", "--layers", ",".join(selected_layers)]
        with self._prepared_pcb_for_export(board, refill_zones=refill_zones) as target_board:
            result = self._export_to_file(["pcb", "export", "svg"], target_board, output_path, extra)
        return _finalize_pcb_export_result(result, board, target_board, refill_zones)

    def export_pcb_render(
        self,
        pcb_path: str,
        output_path: str,
        width: int = 1600,
        height: int = 900,
        side: str = "top",
        quality: str = "basic",
        refill_zones: bool = True,
    ) -> dict[str, Any]:
        """Render a 3D PNG or JPEG view of a `.kicad_pcb` board.

        Args:
            pcb_path: Path to the `.kicad_pcb` board file.
            output_path: Destination image path (`.png` or `.jpg`).
            width: Output image width in pixels.
            height: Output image height in pixels.
            side: Camera view side (`top`, `bottom`, `left`, `right`, `front`, `back`).
            quality: Render quality (`basic`, `high`, `user`, `job_settings`).
            refill_zones: Whether to refill copper zones (safely staged if locked) before render.

        Returns:
            Dictionary with rendered image metadata, zone-refill metadata, and lock info.
        """
        board = _require_existing_path(pcb_path, "PCB file")
        extra = [
            "--width",
            str(width),
            "--height",
            str(height),
            "--side",
            side,
            "--quality",
            quality,
        ]
        with self._prepared_pcb_for_export(board, refill_zones=refill_zones) as target_board:
            result = self._export_to_file(["pcb", "render"], target_board, output_path, extra)
        return _finalize_pcb_export_result(result, board, target_board, refill_zones)

    def _run_json_check(
        self,
        subcommand: list[str],
        input_path: Path,
        output_path: str | None,
        extra_args: list[str] | None = None,
    ) -> tuple[dict[str, Any], str | None]:
        """Execute DRC or ERC with JSON output and return the parsed payload and saved report path.

        Args:
            subcommand: KiCad CLI check subcommand tokens (e.g., `["pcb", "drc"]`).
            input_path: Resolved input PCB or schematic path.
            output_path: Optional file path where the JSON report should be saved.
            extra_args: Optional extra flags passed before `input_path`.

        Returns:
            Tuple of `(parsed_json_dict, saved_report_path_or_none)`.
        """
        with _optional_temp_dir(None if output_path is None else Path(output_path).parent) as work_dir:
            report_file = (
                Path(output_path).expanduser().resolve()
                if output_path is not None
                else work_dir / "report.json"
            )
            args = [
                *subcommand,
                "--output",
                str(report_file),
                "--format",
                "json",
                "--units",
                "mm",
                "--severity-all",
                *(extra_args or []),
                str(input_path),
            ]
            self._run(args)
            data = json.loads(report_file.read_text(encoding="utf-8"))
            return data, str(report_file) if output_path is not None else None

    def run_drc(
        self,
        pcb_path: str,
        output_path: str | None = None,
        schematic_parity: bool = True,
        refill_zones: bool = True,
        save_board: bool = False,
    ) -> dict[str, Any]:
        """Run Design Rules Check (`pcb drc --format json`) and summarize violations.

        Args:
            pcb_path: Path to the `.kicad_pcb` board file.
            output_path: Optional path to persist the raw KiCad JSON DRC report.
            schematic_parity: Whether to include `--schematic-parity` checks against `<stem>.kicad_sch`.
            refill_zones: Whether to refill copper zones (`--refill-zones`) during DRC.
            save_board: Whether to save refilled zones in-place (`--save-board`) when unlocked.

        Returns:
            Dictionary summarizing DRC pass status, violations, parity issues, save status, and lock info.
        """
        board = _require_existing_path(pcb_path, "PCB file")
        lock_info = check_kicad_lock_files(str(board))
        extra_args: list[str] = []
        if schematic_parity:
            extra_args.append("--schematic-parity")
        if refill_zones:
            extra_args.append("--refill-zones")

        board_saved = False
        board_save_skipped_reason: str | None = None
        if save_board:
            if not refill_zones:
                board_save_skipped_reason = "kicad-cli --save-board requires refill_zones=True."
            elif lock_info.get("target_locked"):
                board_save_skipped_reason = (
                    "Target PCB has an active KiCad GUI lock file; skipped --save-board to avoid overwriting an open board."
                )
            else:
                extra_args.append("--save-board")
                board_saved = True

        report, saved_path = self._run_json_check(
            ["pcb", "drc"],
            board,
            output_path,
            extra_args=extra_args,
        )
        violations = report.get("violations", [])
        unconnected = report.get("unconnected_items", [])
        parity = report.get("schematic_parity", [])
        result: dict[str, Any] = {
            "status": "ok",
            "passed": not violations and not unconnected and not parity,
            "kicad_version": report.get("kicad_version", ""),
            "source": report.get("source", str(board)),
            "coordinate_units": report.get("coordinate_units", "mm"),
            "schematic_parity_enabled": schematic_parity,
            "refill_zones_enabled": refill_zones,
            "board_saved": board_saved,
            "violation_count": len(violations),
            "unconnected_count": len(unconnected),
            "schematic_parity_count": len(parity),
            "severity_counts": _count_severities(violations),
            "violations": violations,
            "unconnected_items": unconnected,
            "schematic_parity": parity,
            "report_path": saved_path,
        }
        if board_save_skipped_reason is not None:
            result["board_save_skipped_reason"] = board_save_skipped_reason
        return _attach_lock_info(result, board, lock_info=lock_info)

    def run_erc(self, sch_path: str, output_path: str | None = None) -> dict[str, Any]:
        """Run Electrical Rules Check (`sch erc --format json`) and summarize violations.

        Args:
            sch_path: Path to the `.kicad_sch` schematic file.
            output_path: Optional path to persist the raw KiCad JSON ERC report.

        Returns:
            Dictionary summarizing ERC pass status, sheet violations, and KiCad lock info.
        """
        schematic = _require_existing_path(sch_path, "Schematic file")
        report, saved_path = self._run_json_check(["sch", "erc"], schematic, output_path)
        sheets = report.get("sheets", [])
        violations: list[dict[str, Any]] = []
        for sheet in sheets:
            sheet_path = sheet.get("path", "/")
            for item in sheet.get("violations", []):
                violations.append({"sheet": sheet_path, **item})

        result = {
            "status": "ok",
            "passed": len(violations) == 0,
            "kicad_version": report.get("kicad_version", ""),
            "source": report.get("source", str(schematic)),
            "coordinate_units": report.get("coordinate_units", "mm"),
            "sheet_count": len(sheets),
            "violation_count": len(violations),
            "severity_counts": _count_severities(violations),
            "violations": violations,
            "sheets": sheets,
            "report_path": saved_path,
        }
        return _attach_lock_info(result, schematic)

    def _run_svg_validation(
        self,
        args_builder: Callable[[Path], list[str]],
        target: Path,
        output_dir: str | None,
    ) -> dict[str, Any]:
        """Run an SVG export validation in an isolated staging dir and copy outputs if requested.

        Args:
            args_builder: Callable taking staging `Path` and returning `kicad-cli` argument list.
            target: Resolved path to the footprint or symbol file/library being validated.
            output_dir: Optional directory to preserve generated SVGs.

        Returns:
            Dictionary with `valid` boolean, SVG metadata or error details, and lock info.
        """
        with tempfile.TemporaryDirectory(prefix="kicad_svg_val_") as stage_tmp:
            stage_dir = Path(stage_tmp)
            try:
                proc = self._run(args_builder(stage_dir))
                svg_files = sorted(stage_dir.glob("*.svg"))
                svg_names = [p.name for p in svg_files]
                dest_dir: str | None = None
                if output_dir is not None:
                    out_dir = Path(output_dir).expanduser().resolve()
                    out_dir.mkdir(parents=True, exist_ok=True)
                    for svg_file in svg_files:
                        shutil.copy2(svg_file, out_dir / svg_file.name)
                    dest_dir = str(out_dir)

                result: dict[str, Any] = {
                    "valid": len(svg_names) > 0,
                    "input_path": str(target),
                    "generated_svgs": svg_names if output_dir is not None else [],
                    "svg_count": len(svg_names),
                    "output_dir": dest_dir,
                    "stdout": proc.stdout.strip(),
                }
            except KiCadCLIError as exc:
                result = {
                    "valid": False,
                    "input_path": str(target),
                    "error": str(exc),
                    "returncode": exc.returncode,
                    "stderr": exc.stderr.strip(),
                }
        return _attach_lock_info(result, target)

    def validate_footprint(self, fp_path: str, output_dir: str | None = None) -> dict[str, Any]:
        """Verify KiCad 10 parses a `.kicad_mod` file or `.pretty` library via `fp export svg`.

        Args:
            fp_path: Path to a `.kicad_mod` file or `.pretty` directory.
            output_dir: Optional directory to save exported preview SVGs.

        Returns:
            Validation result dictionary with `valid`, SVG details, and lock info.
        """
        target = _require_existing_path(fp_path, "Footprint or .pretty library")

        if target.is_file() and target.parent.suffix != ".pretty":
            with tempfile.TemporaryDirectory(prefix="kicad_fp_") as tmp_lib:
                pretty_dir = Path(tmp_lib) / "validate.pretty"
                pretty_dir.mkdir()
                shutil.copy2(target, pretty_dir / target.name)
                return self._run_svg_validation(
                    lambda out: [
                        "fp",
                        "export",
                        "svg",
                        "--output",
                        str(out),
                        "--footprint",
                        target.stem,
                        str(pretty_dir),
                    ],
                    target,
                    output_dir,
                )

        if target.is_file():
            return self._run_svg_validation(
                lambda out: [
                    "fp",
                    "export",
                    "svg",
                    "--output",
                    str(out),
                    "--footprint",
                    target.stem,
                    str(target.parent),
                ],
                target,
                output_dir,
            )

        return self._run_svg_validation(
            lambda out: ["fp", "export", "svg", "--output", str(out), str(target)],
            target,
            output_dir,
        )

    def validate_symbol(self, sym_path: str, output_dir: str | None = None) -> dict[str, Any]:
        """Verify KiCad 10 parses a `.kicad_sym` library cleanly via `sym export svg`.

        Args:
            sym_path: Path to a `.kicad_sym` symbol library file.
            output_dir: Optional directory to save exported symbol SVGs.

        Returns:
            Validation result dictionary with `valid`, SVG details, and lock info.
        """
        target = _require_existing_path(sym_path, "Symbol library")
        return self._run_svg_validation(
            lambda out: ["sym", "export", "svg", "--output", str(out), str(target)],
            target,
            output_dir,
        )
