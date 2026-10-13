# Verification and design evidence

Verified on **2026-10-12**, Windows, Python **3.12.10**. Local results below are distinct from
the GitHub Actions workflow, which runs on Linux with Python 3.11 and 3.12.

| Command | Observed result |
| --- | --- |
| `python -m pytest -q -ra` | **276 passed**, zero failures or skips |
| `python -m ruff check src tests examples` | All checks passed |
| `python examples/concurrency_demo.py` | Eight synthetic senders, three turns each: in order, no cross-talk; peak three active turns with three workers |
| `python -m smsbridge inspect "Open Sat-Sun, 9am to noon. Reply STOP to opt out."` | GSM-7, one segment, one part, 49 units |

Commands ran with the repository's `.venv/Scripts/python.exe`; activate a development
environment installed with `python -m pip install -e ".[dev]"` to use the commands above.

## What the tests establish

| Claim | Implementation | Tests |
| --- | --- | --- |
| Per-sender order, cross-sender isolation, bounded concurrency, and fair scheduling | [`scheduler.py`](../src/smsbridge/scheduler.py) | [`test_scheduler.py`](../tests/test_scheduler.py), [`test_concurrency.py`](../tests/test_concurrency.py) |
| The concurrency checks detect deliberately broken shared sessions, sender locks, and duplicate rotation entries | Production scheduler and isolated agent histories | Three planted-defect cases in [`test_concurrency.py`](../tests/test_concurrency.py) |
| Callback/re-check races cannot schedule a second resend for the same failure | [`delivery.py`](../src/smsbridge/delivery.py) | [`test_delivery.py`](../tests/test_delivery.py) |
| Replies are segment-counted and known internal-error/credential shapes are removed or masked | [`gsm7.py`](../src/smsbridge/gsm7.py), [`sanitize.py`](../src/smsbridge/sanitize.py), [`outbound.py`](../src/smsbridge/outbound.py) | [`test_gsm7.py`](../tests/test_gsm7.py), [`test_sanitize.py`](../tests/test_sanitize.py), [`test_outbound.py`](../tests/test_outbound.py) |
| Signed webhooks and agent-backend boundaries behave under failures | [`server.py`](../src/smsbridge/server.py), [`agents.py`](../src/smsbridge/agents.py) | [`test_server.py`](../tests/test_server.py), [`test_agents.py`](../tests/test_agents.py) |

## Scope and limits

- The suite uses Twilio doubles and loopback servers. The concurrency demo signs local
  webhooks with a demo token and uses a dry-run gateway. No real SMS messages are sent.
- Demo timing depends on the machine and synthetic delays; it is not a throughput benchmark
  or a carrier-delivery latency claim.
- Delivery is observed through callbacks and one re-check. An unresolved message is marked
  unconfirmed; successful queuing is not proof of handset receipt.
- Queues, de-duplication, and model history are process-local and in memory. A restart can
  lose acknowledged, unanswered messages; durable multi-replica operation is on the roadmap.
- Sanitization recognizes specific leak shapes and cannot identify all sensitive prose.
- Offline results do not establish live Twilio/model compatibility or deployment readiness.
