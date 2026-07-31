"""WB — WorkBench email+calendar 200-task 한국어 포트 (prereg: analysis/wb-prereg.md §4).

번역 대상은 task 지시문뿐이다. outcome(정답 action 리스트)·도구 스키마·샌드박스
데이터는 바이트 단위 무변경으로 병렬 ground-truth CSV에 실린다. 개체명(사람 이름,
'{subject}'/'{body}'/이벤트명 인용구)은 라틴 표기 그대로 유지한다 — 샌드박스 검색
키가 영어 CSV라 음차하면 풀 수 없는 문제가 된다. 날짜/시간/요일/기간 표현과 문장
골격만 한국어화한다. 번역은 chosen_template 59종(email 26 + calendar 33) 단위로
하고 슬롯을 결정론 치환한다.

출력 (workbench clone의 data/processed/tasks_and_outcomes/ 아래):
- email_ko_tasks_and_outcomes.csv / calendar_ko_tasks_and_outcomes.csv
  (task=한국어, instruction_en/instruction_ko/en_control 컬럼 병기)
- email_enctl_tasks_and_outcomes.csv / calendar_enctl_tasks_and_outcomes.csv
  (영어 대조 30개 — seed 20260723 층화 추출과 동일한 행, task=영어 원문)
- wb_frozen_slice.json (동결 목록 + sha256)

실행: workbench venv로 —
  external/.cache/workbench/.venv/bin/python external/wb_port_ko.py
"""

from __future__ import annotations

import hashlib
import json
import random
import re
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
WB = HERE / ".cache" / "workbench"
TAO = WB / "data" / "processed" / "tasks_and_outcomes"

SEED = 20260723
N_CTL = {"email": 14, "calendar": 16}

# --------------------------------------------------------------------------- #
# 슬롯 값 한국어화 (개체명 제외)
# --------------------------------------------------------------------------- #
_MONTHS = {
    "January": 1, "February": 2, "March": 3, "April": 4, "May": 5, "June": 6,
    "July": 7, "August": 8, "September": 9, "October": 10, "November": 11,
    "December": 12,
}
_DAYS = {"Monday": "월요일", "Tuesday": "화요일", "Wednesday": "수요일",
         "Thursday": "목요일", "Friday": "금요일", "Saturday": "토요일",
         "Sunday": "일요일"}
_BOA = {"before": "이전", "after": "이후"}


def ko_date(v: str) -> str:
    month, day = v.split()
    return f"{_MONTHS[month]}월 {int(day)}일"


def ko_time(v: str) -> str:
    if ":" in v:
        h, m = v.split(":")
        return f"{int(h)}시 {int(m)}분"
    return f"{int(v)}시"


def ko_duration(v: str) -> str:
    """'30 minute'→'30분', '1 hour'→'1시간', '1.5 hour'→'1시간 30분'. 순수 숫자는 그대로."""
    if v.isdigit():
        return v
    num, unit = v.split()
    if unit.startswith("minute"):
        return f"{int(num)}분"
    if num == "1.5":
        return "1시간 30분"
    return f"{int(float(num))}시간"


_SLOT_FILTERS = {
    "natural_language_date": ko_date,
    "natural_language_time": ko_time,
    "duration": ko_duration,
    "next_day": lambda v: _DAYS[v],
    "before_or_after": lambda v: _BOA[v],
}

# --------------------------------------------------------------------------- #
# 템플릿 번역표 — email 26종
# --------------------------------------------------------------------------- #
EMAIL_KO = {
    "Delete my last email from {name}":
        "{name}에게서 온 마지막 이메일을 삭제해 줘",
    "I need to delete my last email from {name}. Can you do that?":
        "{name}에게서 온 마지막 이메일을 삭제해야 해. 해 줄 수 있어?",
    "{name} just sent me an email that I need to delete. Can you get rid of of the most recent email they sent me?":
        "방금 {name}이(가) 보낸 이메일을 삭제해야 해. 그 사람이 나한테 보낸 가장 최근 이메일을 없애 줄래?",
    "Delete all my emails from {name} from the last {days} days":
        "지난 {days}일 동안 {name}에게서 받은 이메일을 전부 삭제해 줘",
    "I need to get rid of all my emails from {name} from the last {days} days. Can you do delete them?":
        "지난 {days}일 동안 {name}에게서 받은 이메일을 전부 없애야 해. 삭제해 줄래?",
    "All my emails from {name} from the last {days} days need to be deleted. Can you do that?":
        "지난 {days}일 동안 {name}에게서 온 이메일이 전부 삭제되어야 해. 해 줄 수 있어?",
    "Send an email to {name} saying '{body}' and title it '{subject}'":
        "{name}에게 '{body}'라는 내용의 이메일을 보내 줘. 제목은 '{subject}'로 해 줘",
    "please send an email to {name} saying '{body}' and title it '{subject}'":
        "{name}에게 '{body}'라는 내용으로 이메일을 보내 줘. 제목은 '{subject}'로 부탁해",
    "I need to send an email to {name} saying '{body}' and title it '{subject}'. Can you do that?":
        "{name}에게 '{body}'라는 내용으로 이메일을 보내야 해. 제목은 '{subject}'로 해 줘. 가능할까?",
    "Reply to {name}'s last email about '{subject}' with 'Thanks for the update - I will get back to you tomorrow.'":
        "{name}이(가) 보낸 '{subject}' 관련 마지막 이메일에 'Thanks for the update - I will get back to you tomorrow.'라고 답장해 줘",
    "I need to get back to {name}'s last email about '{subject}' with 'Thanks for the update - I will get back to you tomorrow. Can you send the reply for me?":
        "{name}이(가) 보낸 '{subject}' 관련 마지막 이메일에 'Thanks for the update - I will get back to you tomorrow.'라고 회신해야 해. 대신 답장을 보내 줄래?",
    "Reply to the latest email from {sender_name} with 'Got it, thank you!'":
        "{sender_name}에게서 온 가장 최근 이메일에 'Got it, thank you!'라고 답장해 줘",
    "can you reply to the latest email from {sender_name} with 'Got it, thank you!'":
        "{sender_name}에게서 온 가장 최근 이메일에 'Got it, thank you!'라고 답장해 줄래?",
    "I need to reply to the latest email from {sender_name} with 'Got it, thank you!'. Can you do that?":
        "{sender_name}에게서 온 가장 최근 이메일에 'Got it, thank you!'라고 답장해야 해. 해 줄 수 있어?",
    "Forward the latest email about '{subject}' to {recipient_name}":
        "'{subject}' 관련 가장 최근 이메일을 {recipient_name}에게 전달해 줘",
    "can you forward the latest email about '{subject}' to {recipient_name}":
        "'{subject}' 관련 가장 최근 이메일을 {recipient_name}에게 전달해 줄래?",
    "{recipient_name} needs the latest email about '{subject}'. Can you forward it?":
        "{recipient_name}이(가) '{subject}' 관련 가장 최근 이메일이 필요하대. 전달해 줄래?",
    "Forward the last email about '{subject}' to {recipient_name1} and {recipient_name2}":
        "'{subject}' 관련 마지막 이메일을 {recipient_name1}와(과) {recipient_name2}에게 전달해 줘",
    "can you forward the last email about '{subject}' to {recipient_name1} and {recipient_name2}":
        "'{subject}' 관련 마지막 이메일을 {recipient_name1}와(과) {recipient_name2}에게 전달해 줄래?",
    "{recipient_name1} and {recipient_name2} need the last email about '{subject}'. Can you forward it?":
        "{recipient_name1}와(과) {recipient_name2}이(가) '{subject}' 관련 마지막 이메일이 필요하대. 전달해 줄래?",
    "Forward my most recent email from {sender_name} to {recipient_name}":
        "{sender_name}에게서 받은 가장 최근 이메일을 {recipient_name}에게 전달해 줘",
    "can you forward my most recent email from {sender_name} to {recipient_name}":
        "{sender_name}에게서 받은 가장 최근 이메일을 {recipient_name}에게 전달해 줄래?",
    "{recipient_name} needs my most recent email from {sender_name}. Can you forward it?":
        "{recipient_name}이(가) 내가 {sender_name}에게서 받은 가장 최근 이메일을 필요로 해. 전달해 줄래?",
    "Forward all the emails from {name} last week about '{subject}' to {recipient_name}":
        "지난주에 {name}에게서 받은 '{subject}' 관련 이메일을 전부 {recipient_name}에게 전달해 줘",
    "can you forward all the emails from {name} last week about '{subject}' to {recipient_name}":
        "지난주에 {name}에게서 받은 '{subject}' 관련 이메일을 전부 {recipient_name}에게 전달해 줄래?",
    "{recipient_name} needs all the emails from {name} last week about '{subject}'. Can you forward them?":
        "{recipient_name}이(가) 지난주에 {name}에게서 온 '{subject}' 관련 이메일을 전부 필요로 해. 전달해 줄래?",
}

# --------------------------------------------------------------------------- #
# 템플릿 번역표 — calendar 33종
# --------------------------------------------------------------------------- #
CAL_KO = {
    "Delete my first meeting on {natural_language_date}":
        "{natural_language_date}의 첫 회의를 삭제해 줘",
    "Cancel my first meeting on {natural_language_date}":
        "{natural_language_date}의 첫 회의를 취소해 줘",
    "can you cancel my first meeting on {natural_language_date}":
        "{natural_language_date}의 첫 회의를 취소해 줄래?",
    "Change the name of the last event on {natural_language_date} to {event_name}":
        "{natural_language_date} 마지막 일정의 이름을 {event_name}(으)로 바꿔 줘",
    "Can you change the name of the last event on {natural_language_date} to {event_name}":
        "{natural_language_date} 마지막 일정의 이름을 {event_name}(으)로 바꿔 줄래?",
    "Rename the last event on {natural_language_date} to {event_name}":
        "{natural_language_date} 마지막 일정 이름을 {event_name}(으)로 변경해 줘",
    "Rename the next {event_name} meeting to {new_event_name}":
        "다음 {event_name} 회의의 이름을 {new_event_name}(으)로 바꿔 줘",
    "can you rename the next {event_name} meeting to {new_event_name}":
        "다음 {event_name} 회의 이름을 {new_event_name}(으)로 바꿔 줄래?",
    "Change the name of the next {event_name} meeting to {new_event_name}":
        "다음 {event_name} 회의의 이름을 {new_event_name}(으)로 변경해 줘",
    "Cancel my next meeting with {name}":
        "{name}와(과)의 다음 회의를 취소해 줘",
    "I need to cancel my next meeting with {name}. Can you do that for me please?":
        "{name}와(과)의 다음 회의를 취소해야 해요. 처리해 주시겠어요?",
    "{name} is off sick. Can you cancel my next meeting with them?":
        "{name}이(가) 아파서 쉰대. 그 사람과의 다음 회의를 취소해 줄래?",
    "Cancel all future meetings with {name}":
        "{name}와(과)의 앞으로의 회의를 전부 취소해 줘",
    "I need to cancel all future meetings with {name}. Can you do that for me please?":
        "{name}와(과)의 앞으로의 회의를 전부 취소해야 해요. 처리해 주시겠어요?",
    "{name} is leaving the company. Can you cancel all future meetings with them?":
        "{name}이(가) 퇴사한대. 그 사람과의 앞으로의 회의를 전부 취소해 줄래?",
    "Cancel the next {event_name} meeting":
        "다음 {event_name} 회의를 취소해 줘",
    "Can you cancel the next {event_name} meeting":
        "다음 {event_name} 회의를 취소해 줄래?",
    "Delete the next {event_name} meeting":
        "다음 {event_name} 회의를 삭제해 줘",
    "Cancel future {event_name} meetings":
        "앞으로의 {event_name} 회의를 취소해 줘",
    "Delete all the future {event_name} meetings":
        "앞으로 있을 {event_name} 회의를 전부 삭제해 줘",
    "We've decided we don't need any any more {event_name} meetings. Can you cancel all future ones?":
        "{event_name} 회의는 더 이상 안 하기로 했어. 앞으로 잡힌 것들을 전부 취소해 줄래?",
    "please move my first meeting with {name} on {natural_language_date} by {duration}s":
        "{natural_language_date}에 있는 {name}와(과)의 첫 회의를 {duration}만큼 옮겨 줘",
    "Push back my first meeting with {name} on {natural_language_date} by {duration}s":
        "{natural_language_date}에 있는 {name}와(과)의 첫 회의를 {duration} 미뤄 줘",
    "Delay my first meeting with {name} on {natural_language_date} by {duration}s":
        "{natural_language_date}에 있는 {name}와(과)의 첫 회의를 {duration} 늦춰 줘",
    "something came up. Can you cancel my meetings on {next_day} {before_or_after} {natural_language_time}?":
        "일이 생겼어. {next_day} {natural_language_time} {before_or_after} 회의를 다 취소해 줄래?",
    "Cancel my meetings on {next_day} {before_or_after} {natural_language_time}":
        "{next_day} {natural_language_time} {before_or_after} 회의를 전부 취소해 줘",
    "Delete my meetings on {next_day} {before_or_after} {natural_language_time}":
        "{next_day} {natural_language_time} {before_or_after} 회의를 전부 삭제해 줘",
    "I need to catch up with {name}. can you schedule a {duration} event called {event_name} on {natural_language_date} at {natural_language_time}?":
        "{name}와(과) 얘기 좀 해야 해. {natural_language_date} {natural_language_time}에 {event_name}(이)라는 {duration}짜리 일정을 잡아 줄래?",
    "I haven't met with {name} in a while. Can you schedule a {duration} event called {event_name} on {natural_language_date} at {natural_language_time}?":
        "{name}와(과) 못 본 지 꽤 됐어. {natural_language_date} {natural_language_time}에 {event_name}(이)라는 {duration}짜리 일정을 잡아 줄래?",
    "Create a {duration} event called {event_name} on {natural_language_date} at {natural_language_time} with {name}":
        "{natural_language_date} {natural_language_time}에 {name}와(과) 함께하는 {event_name}(이)라는 {duration}짜리 일정을 만들어 줘",
    "If I haven't met with {name} in the last {duration} days, schedule a 30-minute meeting called 'catch-up' for my first free slot from tomorrow":
        "지난 {duration}일 동안 {name}와(과) 회의한 적이 없으면, 내일부터 가장 빠른 빈 시간대에 'catch-up'이라는 30분짜리 회의를 잡아 줘",
    "have I met with {name} in the last {duration} days? If not, schedule a 30-minute meeting called 'catch-up' for my first free slot from tomorrow":
        "내가 지난 {duration}일 동안 {name}와(과) 회의한 적이 있어? 없다면 내일부터 가장 빠른 빈 시간대에 'catch-up'이라는 30분짜리 회의를 잡아 줘",
    "I think I might need to catch up with {name}. Can you check if I've met with them in the last {duration} days? If not, schedule a 30-minute meeting for my first free slot from tomorrow":
        "{name}와(과) 한번 얘기해야 할 것 같아. 지난 {duration}일 동안 그 사람과 회의한 적이 있는지 확인해 줄래? 없었다면 내일부터 가장 빠른 빈 시간대에 30분짜리 회의를 잡아 줘",
}

KO_BY_DOMAIN = {"email": EMAIL_KO, "calendar": CAL_KO}


def extract_slots(template: str, task: str) -> dict[str, str]:
    pat = re.escape(template)
    for slot in set(re.findall(r"\\\{(\w+)\\\}", pat)):
        pat = pat.replace(r"\{" + slot + r"\}", f"(?P<{slot}>.+?)")
    m = re.fullmatch(pat, task, re.DOTALL)
    if m is None:
        raise ValueError(f"slot extract failed: {template!r} vs {task!r}")
    return m.groupdict()


def translate(domain: str, template: str, task: str) -> str:
    ko_tpl = KO_BY_DOMAIN[domain][template]
    slots = extract_slots(template, task)
    out = ko_tpl
    for k, v in slots.items():
        out = out.replace("{" + k + "}", _SLOT_FILTERS.get(k, lambda x: x)(v))
    assert "{" not in out.replace("{'", "{'"), f"unfilled slot in: {out}"
    return out


def main() -> None:
    rng = random.Random(SEED)
    frozen: dict = {"seed": SEED, "domains": {}}
    for domain in ["email", "calendar"]:
        src = TAO / f"{domain}_tasks_and_outcomes.csv"
        df = pd.read_csv(src, dtype=str)
        df["instruction_en"] = df["task"]
        df["instruction_ko"] = [
            translate(domain, r["chosen_template"], r["task"]) for _, r in df.iterrows()
        ]
        assert df["instruction_ko"].nunique() == len(df), f"{domain}: ko collision"

        # 영어 대조군: 템플릿 층화(템플릿 순회하며 1개씩) — seed 고정
        by_tpl: dict[str, list[int]] = {}
        for i, tpl in enumerate(df["chosen_template"]):
            by_tpl.setdefault(tpl, []).append(i)
        tpl_order = sorted(by_tpl)
        rng.shuffle(tpl_order)
        picked: list[int] = []
        while len(picked) < N_CTL[domain]:
            for tpl in tpl_order:
                if by_tpl[tpl] and len(picked) < N_CTL[domain]:
                    picked.append(by_tpl[tpl].pop(rng.randrange(len(by_tpl[tpl]))))
        df["en_control"] = [i in set(picked) for i in range(len(df))]

        ko = df.copy()
        ko["task"] = ko["instruction_ko"]
        ko_path = TAO / f"{domain}_ko_tasks_and_outcomes.csv"
        ko.to_csv(ko_path, index=False)

        ctl = df[df["en_control"]].copy()  # task = 영어 원문 그대로
        ctl_path = TAO / f"{domain}_enctl_tasks_and_outcomes.csv"
        ctl.to_csv(ctl_path, index=False)

        frozen["domains"][domain] = {
            "n_tasks": len(df),
            "n_en_control": int(df["en_control"].sum()),
            "src_sha256": hashlib.sha256(src.read_bytes()).hexdigest(),
            "ko_sha256": hashlib.sha256(ko_path.read_bytes()).hexdigest(),
            "enctl_sha256": hashlib.sha256(ctl_path.read_bytes()).hexdigest(),
            "en_control_tasks": ctl["instruction_en"].tolist(),
        }
        print(f"{domain}: {len(df)} ko tasks, {len(ctl)} en-control -> {ko_path.name}")

    out = HERE / "wb_frozen_slice.json"
    out.write_text(json.dumps(frozen, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"frozen slice manifest -> {out}")


if __name__ == "__main__":
    main()
