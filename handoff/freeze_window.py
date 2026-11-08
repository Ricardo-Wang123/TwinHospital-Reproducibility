"""Freeze a new dispatch window without changing clinical runtime parameters."""

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path

from sandbox.experiments.linked.campaign import validate_window


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--template', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--workers', type=int, choices=range(1, 33))
    parser.add_argument('--dispatch-hours', type=float, default=72)
    args = parser.parse_args()
    if not 0 < args.dispatch_hours <= 168:
        parser.error('Dispatch hours must be in (0, 168].')
    window = json.loads(args.template.read_text())
    now = datetime.now(timezone.utc)
    window['frozen_at'] = now.isoformat()
    window['submission_window'].update(opens_at=now.isoformat(),
                                       closes_at=(now + timedelta(hours=args.dispatch_hours)).isoformat())
    if args.workers is not None:
        window['workers'] = args.workers
    validate_window(window, workers=window['workers'], now=now)
    args.output.parent.mkdir(mode=0o700, exist_ok=True, parents=True)
    with args.output.open('x', encoding='utf-8') as stream:
        json.dump(window, stream, indent=2)
        stream.write('\n')
    print(f'Dispatch window frozen: {args.output}')


if __name__ == '__main__':
    main()
