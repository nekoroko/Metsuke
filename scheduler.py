# scheduler.py — Type 1/Type 2両対応スケジューラ
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from datetime import datetime
from db import get_all_enabled_schedules, get_tool, get_agent_task, add_execution
from executor import run_tool, run_agent, run_agent_background

scheduler = BackgroundScheduler()

def _execute_scheduled(exec_type: str, target_id: str, schedule_id: str):
    """スケジュール実行のディスパッチ"""
    if exec_type == "tool":
        tool = get_tool(target_id)
        if tool and tool["status"] == "verified":
            print(f"[scheduler] ツール実行: {tool['name']}")
            run_tool(tool["id"], tool["name"], tool["code"],
                    trigger="schedule", schedule_id=schedule_id)
        else:
            print(f"[scheduler] ツールが見つからないか未検証: {target_id}")

    elif exec_type == "agent":
        task = get_agent_task(target_id)
        if task:
            print(f"[scheduler] エージェント実行: {task['name']}")
            # task_idも渡してallowed_tool_idsを利用可能にする
            exec_id = add_execution("agent", task["id"], task["name"],
                                   trigger="schedule", schedule_id=schedule_id)
            run_agent_background(exec_id, task["task_prompt"], task["id"])
        else:
            print(f"[scheduler] タスクが見つかりません: {target_id}")

def load_schedules():
    """DBからスケジュールを読み込んでジョブに登録する"""
    schedules = get_all_enabled_schedules()
    for s in schedules:
        try:
            job_id = f"sched_{s['id']}"
            if scheduler.get_job(job_id):
                continue
            trigger = CronTrigger.from_crontab(s["cron_expr"])
            scheduler.add_job(
                _execute_scheduled,
                trigger=trigger,
                id=job_id,
                args=[s["exec_type"], s["target_id"], s["id"]],
                replace_existing=True,
            )
            print(f"[scheduler] ジョブ登録: {s['exec_type']}:{s['target_id']} ({s['cron_expr']})")
        except Exception as e:
            print(f"[scheduler] ジョブ登録エラー: {e}")

def add_job(exec_type: str, target_id: str, cron_expr: str, schedule_id: str) -> bool:
    try:
        job_id = f"sched_{schedule_id}"
        trigger = CronTrigger.from_crontab(cron_expr)
        scheduler.add_job(
            _execute_scheduled,
            trigger=trigger,
            id=job_id,
            args=[exec_type, target_id, schedule_id],
            replace_existing=True,
        )
        return True
    except Exception as e:
        print(f"[scheduler] ジョブ追加エラー: {e}")
        return False

def remove_job(schedule_id: str):
    job_id = f"sched_{schedule_id}"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)

def run_agent_now(task_id: str, task_name: str, task_prompt: str) -> str:
    """
    エージェントタスクを即時バックグラウンド実行する。
    """
    exec_id = add_execution("agent", task_id, task_name, trigger="manual")
    scheduler.add_job(
        run_agent_background,
        trigger='date',
        run_date=datetime.now(),
        args=[exec_id, task_prompt, task_id],
        id=f"now_{exec_id}",
        replace_existing=False,
    )
    return exec_id


def start():
    if scheduler.running:
        return
    load_schedules()
    scheduler.start()
    print("[scheduler] スケジューラ起動")

def shutdown():
    if scheduler.running:
        scheduler.shutdown()