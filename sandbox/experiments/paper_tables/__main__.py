"""Commands for preparing inputs, running experiments and exporting tables."""
from __future__ import annotations

import argparse
import json
import runpy
import sys
from pathlib import Path

ROUTES = {
    'configure': 'configurations', 'import': 'imports', 'analyze': 'analysis',
    'native-materials': 'native_materials', 'native': 'native_execution',
}


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='TwinHospital: input cases, clinical/native experiments and 17 tables.',
        epilog='prepare/export are offline; clinical calls models; native can write hospital records.')
    parser.add_argument('command', choices=['prepare', 'clinical', 'export', *ROUTES])
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments or arguments[0] in ('-h', '--help'):
        parser.print_help()
        return
    args = parser.parse_args(arguments[:1])
    remaining = arguments[1:]
    if args.command == 'prepare':
        sub = argparse.ArgumentParser(description='Generate constructed cases without API calls.')
        sub.add_argument('--output', required=True)
        sub.add_argument('--seed', type=int, default=20261025)
        options = sub.parse_args(remaining)
        target = Path(options.output)
        if target.exists():
            sub.error('output exists; choose a new directory')
        from .materials import prepare
        result = prepare(target, seed=options.seed)
        print(json.dumps({'dataset': str(result.resolve()), 'source_kind': 'engineering_example',
                          'clinical_validation': False}, ensure_ascii=False))
    elif args.command == 'clinical':
        from .run import main as clinical_main
        clinical_main(['clinical-batch', *remaining])
    elif args.command == 'export':
        sub = argparse.ArgumentParser(description='Calculate 17 CSV tables from saved rows; no APIs.')
        sub.add_argument('--observations', required=True)
        sub.add_argument('--context', required=True)
        sub.add_argument('--output', required=True)
        options = sub.parse_args(remaining)
        target = Path(options.output)
        if target.exists():
            sub.error('output exists; choose a new directory')
        from .table_export import export_tables
        observations = json.loads(Path(options.observations).read_text(encoding='utf-8-sig'))
        context = json.loads(Path(options.context).read_text(encoding='utf-8-sig'))
        print(json.dumps(export_tables(target, observations, context), ensure_ascii=False))
    else:
        sys.argv = [parser.prog + ' ' + args.command, *remaining]
        runpy.run_module(__package__ + '.' + ROUTES[args.command], run_name='__main__')


if __name__ == '__main__':
    main()
