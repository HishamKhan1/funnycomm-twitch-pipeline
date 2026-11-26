"""Invoked by Windows Task Scheduler. Never publishes publicly.
Discovers, downloads and edits for local human review. Uploads only items the
user already approved: private, or private until the publish time the user chose.
"""
import traceback
from datetime import datetime
from pipeline_core import (REPORTS, RunLock, settings, Twitch, record_sources, download_new, production_rows, upload_private, daily_backup, drive_warnings, recheck_downloads)


def run():
    cf = settings()
    path = REPORTS / ('daily_' + datetime.now().strftime('%Y-%m-%d') + '.log')
    with path.open('a', encoding='utf-8') as out:
        def info(v):
            print(datetime.now().isoformat(timespec='seconds'), v, file=out, flush=True)
        info('Daily run started: never publishes videos publicly')
        try:
            with RunLock():
                daily_backup()
                w = drive_warnings()
                if w >= 6:
                    info(f'STOPPED: Windows logged {w} disk errors in 24h. Check the external drive before heavy work.')
                    return
                import pipeline_render as pr
                off = set(cf.get('streamers_off') or [])
                who = [w for w in cf['streamers'] if w not in off]
                waiting = sum(p['state'] == 'review' and p['kind'] != 'video' for p in production_rows())
                want = max(0, int(cf.get('posts_per_day') or 20) - waiting)
                if not cf.get('permission_confirmed'):
                    info('Nothing made: no rights confirmation in Settings')
                elif want and who:
                    info(f'{pr.make_batch(cf, want, who, cf.get("clip_mix", "mix"), info)} new Shorts are waiting for review')
            # Only what the owner watched and approved is uploaded, at the time the owner's settings gave it.
            import pipeline_youtube as yt
            for _ in range(100):
                if not yt.post_next(cf, info):
                    break
            info('Daily run finished')
        except Exception:
            info('ERROR ' + traceback.format_exc())


if __name__ == '__main__':
    run()
