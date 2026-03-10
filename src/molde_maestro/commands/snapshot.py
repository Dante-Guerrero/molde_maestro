from __future__ import annotations

from pathlib import Path

from .. import pipeline as core
from ..command_config import SnapshotCommandConfig


def cmd_snapshot(args) -> None:
    config = SnapshotCommandConfig.from_args(args)
    repo, ai_dir, recorder = core.init_run_context(config._raw_args)
    core.preflight(config._raw_args, repo)

    if not config.zip_enabled:
        print("snapshot: --zip no está activado; no se creó zip.")
        recorder.mark_stage_skipped("snapshot", "zip_disabled")
        recorder.complete_run("ok", {"snapshot_created": False})
        return

    with core.record_stage(recorder, "snapshot") as details:
        out_zip = Path(config.out).expanduser() if config.out else ai_dir / "snapshots" / f"{core.now_stamp()}.zip"
        if not out_zip.is_absolute():
            out_zip = repo / out_zip

        core.git_archive_zip(repo, out_zip, ref=config.ref)
        details["artifact"] = str(out_zip)
        print(f"snapshot: wrote {out_zip}")
    recorder.complete_run("ok", {"snapshot_created": True})
