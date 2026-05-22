"""
G-seff — GPU Slurm Efficiency Reporter
FastAPI backend for OOD deployment.
"""

import subprocess
import logging
from datetime import datetime, timedelta
from collections import defaultdict
from typing import Optional
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
import requests as http

log = logging.getLogger(__name__)

SACCT_BIN            = "/opt/slurm/bin/sacct"
SCONTROL_BIN         = "/opt/slurm/bin/scontrol"
PROMETHEUS_URL       = "http://hpc-monitor1:9090"
DCGM_METRIC          = "DCGM_FI_DEV_GPU_UTIL"
EFFICIENCY_THRESHOLD = 30
ADMIN_GROUP          = "hpc_eeadmins"
DAYS                 = 15
OOD_USER_HEADER      = "X-Remote-User"


def is_admin(username: str) -> bool:
    try:
        result = subprocess.run(["id", username], capture_output=True, text=True)
        return ADMIN_GROUP in result.stdout
    except Exception:
        return False

app = FastAPI(title="G-seff", root_path="/gseff")
templates = Jinja2Templates(directory="templates")


def get_jobs_sacct(user: Optional[str] = None) -> list[dict]:
    """Pull completed GPU jobs from sacct. CPU/mem from .batch step."""
    start = (datetime.now() - timedelta(days=DAYS)).strftime("%Y-%m-%dT%H:%M:%S")

    cmd = [
        SACCT_BIN, "--allusers", "--starttime", start,
        "--format", "JobID,User,NodeList,Partition,Start,End,AllocTRES,AllocCPUS,AveCPU,ReqMem,MaxRSS,State",
        "--parsable2", "--noheader",
    ]
    if user:
        cmd = [c for c in cmd if c != "--allusers"]
        cmd += ["--user", user]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        log.error("sacct failed: %s", result.stderr)
        return []

    parent_jobs = {}
    batch_data  = {}

    for line in result.stdout.strip().splitlines():
        parts = line.split("|")
        if len(parts) < 12:
            continue

        job_id, user_, nodelist, partition, start_s, end_s, alloctres, alloccpus, avecpu, reqmem, maxrss, state = parts[:12]

        # Collect .batch step for CPU/mem (parent job line has empty AveCPU/MaxRSS)
        if job_id.endswith(".batch"):
            parent_id = job_id.split(".")[0]
            batch_data[parent_id] = (avecpu, alloccpus, maxrss, reqmem)
            continue

        if "." in job_id:
            continue

        if not any(s in state for s in ("COMPLETED", "FAILED", "TIMEOUT")):
            continue
        if "Unknown" in (start_s, end_s) or not start_s or not end_s:
            continue

        gpus = _parse_gpus(alloctres)
        if gpus == 0:
            continue

        try:
            start_dt = datetime.strptime(start_s, "%Y-%m-%dT%H:%M:%S")
            end_dt   = datetime.strptime(end_s,   "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            continue

        if end_dt <= start_dt:
            continue

        nodes = _expand_nodelist(nodelist)

        parent_jobs[job_id] = {
            "job_id":     job_id,
            "user":       user_,
            "nodes":      nodes,
            "start":      start_dt,
            "end":        end_dt,
            "gpus":       gpus,
            "cpu_eff":    None,
            "mem_eff":    None,
            "duration_h": (end_dt - start_dt).total_seconds() / 3600,
            "partition":  partition,
            "gpu_eff":    None,
            "flagged":    False,
            "reqmem":     reqmem,
        }

    # Enrich with batch step CPU/mem
    for job_id, job in parent_jobs.items():
        if job_id in batch_data:
            avecpu, alloccpus, maxrss, _ = batch_data[job_id]
            job["cpu_eff"] = _calc_cpu_eff(avecpu, alloccpus, job["start"], job["end"])
            job["mem_eff"] = _calc_mem_eff(maxrss, job["reqmem"])

    return list(parent_jobs.values())


def _parse_gpus(alloctres: str) -> int:
    for part in alloctres.split(","):
        if "gres/gpu=" in part:
            try:
                return int(part.split("=")[1])
            except (IndexError, ValueError):
                pass
    return 0


def _parse_int(s: str) -> Optional[int]:
    try:
        return int(s.strip())
    except (ValueError, AttributeError):
        return None


def _expand_nodelist(nodelist: str) -> list[str]:
    if not nodelist or nodelist in ("None assigned", ""):
        return []
    result = subprocess.run(
        [SCONTROL_BIN, "show", "hostnames", nodelist],
        capture_output=True, text=True
    )
    return result.stdout.strip().splitlines() if result.returncode == 0 else [nodelist]


def _calc_cpu_eff(avecpu: str, alloccpus: str, start: datetime, end: datetime) -> Optional[float]:
    try:
        cpus = int(alloccpus.strip())
        wall = (end - start).total_seconds()
        if wall <= 0 or cpus <= 0:
            return None
        cpu_secs = _parse_slurm_duration(avecpu.strip())
        if cpu_secs is None:
            return None
        return min(100.0, cpu_secs / (cpus * wall) * 100)
    except Exception:
        return None


def _parse_slurm_duration(s: str) -> Optional[float]:
    try:
        days = 0
        if "-" in s:
            d, s = s.split("-", 1)
            days = int(d)
        parts = s.split(":")
        if len(parts) == 3:
            h, m, sec = int(parts[0]), int(parts[1]), float(parts[2])
            return days * 86400 + h * 3600 + m * 60 + sec
    except Exception:
        pass
    return None


def _calc_mem_eff(maxrss: str, reqmem: str) -> Optional[float]:
    try:
        used  = _parse_mem_bytes(maxrss.strip())
        total = _parse_mem_bytes(reqmem.strip())
        if used is None or total is None or total == 0:
            return None
        return min(100.0, used / total * 100)
    except Exception:
        return None


def _parse_mem_bytes(s: str) -> Optional[int]:
    if not s or s in ("", "0"):
        return None
    multipliers = {"K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}
    suffix = s[-1].upper()
    if suffix in multipliers:
        try:
            return int(float(s[:-1]) * multipliers[suffix])
        except ValueError:
            return None
    try:
        return int(s)
    except ValueError:
        return None


def enrich_gpu(jobs: list[dict]) -> list[dict]:
    """One Prometheus range query per node, match to jobs by time window."""
    if not jobs:
        return jobs

    node_jobs: dict[str, list] = defaultdict(list)
    for job in jobs:
        if job["nodes"]:
            node_jobs[job["nodes"][0]].append(job)

    global_start = min(j["start"] for j in jobs)
    global_end   = max(j["end"]   for j in jobs)
    step = 3600  # 1-hour resolution — 720 points per node over 30 days

    session = http.Session()
    session.trust_env = False

    for node, node_job_list in node_jobs.items():
        query = f'{DCGM_METRIC}{{Hostname="{node}"}}'
        try:
            resp = session.get(
                f"{PROMETHEUS_URL}/api/v1/query_range",
                params={
                    "query": query,
                    "start": global_start.timestamp(),
                    "end":   global_end.timestamp(),
                    "step":  step,
                },
                timeout=60,
            )
            resp.raise_for_status()
            data = resp.json().get("data", {}).get("result", [])
            if not data:
                continue

            # Average across all GPU devices on this node at each timestamp
            ts_vals: dict = defaultdict(list)
            for series in data:
                for ts, val in series["values"]:
                    try:
                        ts_vals[float(ts)].append(float(val))
                    except (ValueError, TypeError):
                        pass

            timeline = sorted((ts, sum(v)/len(v)) for ts, v in ts_vals.items())
            if not timeline:
                continue

            for job in node_job_list:
                job_start_ts = job["start"].timestamp()
                job_end_ts   = job["end"].timestamp()
                points = [v for ts, v in timeline if job_start_ts <= ts <= job_end_ts]
                if points:
                    job["gpu_eff"] = round(sum(points) / len(points), 1)
                    job["flagged"] = job["gpu_eff"] < EFFICIENCY_THRESHOLD

        except Exception as e:
            log.warning("Prometheus range query failed for node %s: %s", node, e)

    return jobs


def serialize_job(j: dict) -> dict:
    return {
        "job_id":     j["job_id"],
        "user":       j["user"],
        "partition":  j.get("partition", ""),
        "nodes":      j["nodes"],
        "gpus":       j["gpus"],
        "gpu_eff":    round(j["gpu_eff"], 1) if j["gpu_eff"] is not None else None,
        "cpu_eff":    round(j["cpu_eff"], 1) if j["cpu_eff"] is not None else None,
        "mem_eff":    round(j["mem_eff"], 1) if j["mem_eff"] is not None else None,
        "duration_h": round(j["duration_h"], 2),
        "flagged":    j["flagged"],
    }


def avg(vals):
    v = [x for x in vals if x is not None]
    return round(sum(v) / len(v), 1) if v else None


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    username = request.headers.get(OOD_USER_HEADER, "unknown")
    user_is_admin = is_admin(username)
    return templates.TemplateResponse("gseff.html", {
        "request":  request,
        "username": username,
        "is_admin": user_is_admin,
    })


@app.get("/api/me")
async def my_jobs(request: Request):
    username = request.headers.get(OOD_USER_HEADER)
    if not username:
        raise HTTPException(status_code=401, detail="No OOD session")

    jobs = get_jobs_sacct(user=username)
    jobs = enrich_gpu(jobs)

    return {
        "user":  username,
        "days":  DAYS,
        "summary": {
            "job_count":   len(jobs),
            "avg_gpu_eff": avg([j["gpu_eff"] for j in jobs if j["gpu_eff"] is not None]),
            "avg_cpu_eff": avg([j["cpu_eff"] for j in jobs if j["cpu_eff"] is not None]),
            "avg_mem_eff": avg([j["mem_eff"] for j in jobs if j["mem_eff"] is not None]),
            "flagged":     sum(1 for j in jobs if j["flagged"]),
            "gpu_hours":   round(sum(j["gpus"] * j["duration_h"] for j in jobs), 1),
        },
        "jobs": [serialize_job(j) for j in sorted(jobs, key=lambda x: (x["gpu_eff"] or 999))],
    }


@app.get("/api/all")
async def all_users(request: Request):
    username = request.headers.get(OOD_USER_HEADER)
    if not username:
        raise HTTPException(status_code=401, detail="No OOD session")
    if not is_admin(username):
        raise HTTPException(status_code=403, detail="Admins only")

    jobs = get_jobs_sacct()
    jobs = enrich_gpu(jobs)

    user_map: dict[str, dict] = {}
    for j in jobs:
        u = user_map.setdefault(j["user"], {
            "user": j["user"], "jobs": 0,
            "gpu_effs": [], "cpu_effs": [], "mem_effs": [],
            "flagged": 0, "gpu_hours": 0.0,
        })
        u["jobs"] += 1
        if j["gpu_eff"] is not None: u["gpu_effs"].append(j["gpu_eff"])
        if j["cpu_eff"] is not None: u["cpu_effs"].append(j["cpu_eff"])
        if j["mem_eff"] is not None: u["mem_effs"].append(j["mem_eff"])
        if j["flagged"]: u["flagged"] += 1
        u["gpu_hours"] += j["gpus"] * j["duration_h"]

    users = sorted([{
        "user":        u["user"],
        "jobs":        u["jobs"],
        "avg_gpu_eff": avg(u["gpu_effs"]),
        "avg_cpu_eff": avg(u["cpu_effs"]),
        "avg_mem_eff": avg(u["mem_effs"]),
        "flagged":     u["flagged"],
        "gpu_hours":   round(u["gpu_hours"], 1),
        "is_flagged":  (avg(u["gpu_effs"]) or 100) < EFFICIENCY_THRESHOLD,
    } for u in user_map.values()], key=lambda x: (x["avg_gpu_eff"] or 999))

    all_gpu = [j["gpu_eff"] for j in jobs if j["gpu_eff"] is not None]
    wasted  = sum(j["gpus"] * j["duration_h"] * max(0, (100 - (j["gpu_eff"] or 100)) / 100) for j in jobs)

    return {
        "days": DAYS,
        "summary": {
            "total_jobs":       len(jobs),
            "avg_gpu_eff":      avg(all_gpu),
            "flagged_users":    sum(1 for u in users if u["is_flagged"]),
            "gpu_hours_wasted": round(wasted, 1),
        },
        "users": users,
        "jobs":  [serialize_job(j) for j in sorted(jobs, key=lambda x: (x["gpu_eff"] or 999))],
    }
