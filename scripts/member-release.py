#!/usr/bin/env python3
"""阶段中只释放一个已交接成员，不执行整项收尾。会话标识取自本轮领取或收取记录。"""

import argparse
from pathlib import Path
import re
import runpy
import sys

wr = runpy.run_path(str(Path(__file__).with_name('wrapup.py')))


def release(iid, pane, session):
    owner = wr['current_scheduler'](None)
    info = wr['herdr']('agent', 'get', pane)['result']['agent']
    entry = wr['_MS'].read_registry().get(pane, {})
    profile = entry.get('profile')
    if not (profile and entry.get('scheduler') == owner and info.get('pane_id') == pane
            and entry.get('tab_id') and entry['tab_id'] == info.get('tab_id')
            and re.fullmatch(rf'i{iid}-{re.escape(profile)}(?:-\d+)?', info.get('name') or '')):
        raise ValueError('当前窗格与本任务的成员登记不一致，保留')
    if not session or (info.get('agent_session') or {}).get('value') != session:
        raise ValueError('成员会话已变化或无法确认，保留')
    if info.get('agent_status') not in ('idle', 'done') or not info.get('interactive_ready'):
        raise ValueError('成员未空闲或尚未就绪，保留')
    result = wr['close_members']([info], False)
    print('\n'.join(result.closed + result.kept + result.problems))
    return 0 if result.closed and not result.problems else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--issue', type=int, required=True)
    parser.add_argument('--pane', required=True)
    parser.add_argument('--session', required=True)
    args = parser.parse_args()
    try:
        sys.exit(release(args.issue, args.pane, args.session))
    except (RuntimeError, KeyError, ValueError, OSError) as error:
        sys.exit(str(error))
