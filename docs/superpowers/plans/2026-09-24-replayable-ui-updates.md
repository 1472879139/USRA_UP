# Replayable UI Updates Implementation Plan

> **For agentic workers:** Implement inline, task by task, with red/green tests. This workspace has no `.git` directory, so do not run commit commands.

**Goal:** Keep the Flask UI synchronized with a running analysis and replay the current session's chat events after a browser refresh.

**Architecture:** `ui_queue` stays the producer boundary; `URSAGUI.get_updates(since)` drains it under a lock into a current-session append-only event list. Each poll returns a cursor-filtered event slice and a canonical state snapshot. Browser rendering uses only server events, one in-flight poll, and a busy-aware submit path.

**Tech Stack:** Python 3.10/unittest, Flask, browser JavaScript, Node 24 built-in test runner.

---

### Task 1: Replayable server event feed

**Files:** Modify `gui/app.py` (`__init__`, `get_updates`, `_process_updates`, `_build_state_response`, `/api/updates`); create `tests/test_ui_updates.py`.

- [ ] Write an offline `unittest` fixture using `object.__new__(URSAGUI)`, a real `Session`, a `Queue`, and a `threading.RLock`. Enqueue one System message. Assert `get_updates(since=0)` twice returns that same event with the same positive `seq`, while `get_updates(since=seq)` returns no events.
- [ ] Run `D:\miniconda3\envs\ursa-up\python.exe -m unittest discover -s tests -p test_ui_updates.py -v`; expect failure because `get_updates` has no `since` argument.
- [ ] Add `_ui_lock`, `_ui_events` and monotonic `_ui_seq` to `URSAGUI`. In `get_updates(since=0)`, lock, drain and process the queue once, assign each new visible update a sequence number, append it, then return copies where `seq > max(0, since)`. Change `/api/updates` to pass `request.args.get('since', 0, type=int)`.
- [ ] Run the focused test; expect pass. Add a second test proving two different cursors do not consume each other's messages, then verify it passes.

### Task 2: Canonical state and safe busy response

**Files:** Modify `gui/app.py` (`_build_state_response`, `start_analysis`, flow exception handling); extend `tests/test_ui_updates.py`.

- [ ] Add tests for a running Session with an old queued `done`: response must still say `running=True`, `done=False`, and show the current phase. Add tests for `MANAGER_CONFIRM`, `COMPLETED`, and `TERMINATED` when not running. Add a test that a busy `start_analysis` response includes `busy=True` and does not start a new thread.
- [ ] Run the focused tests and observe the expected failures.
- [ ] Derive `phase`, `running`, `done`, `waiting_confirm`, and `status` from `Session.state_machine` plus `_running`, not from the drained event batch. Set `done=True` only when no task runs and the phase is confirmation/completed/terminated. Return `busy=True` on the existing busy rejection path. Ensure exceptions terminate the Session after recording an error event.
- [ ] Run the focused tests; expect pass.

### Task 3: One server-owned user event per accepted request

**Files:** Modify `gui/app.py` (`start_analysis`, `_process_updates`); extend `tests/test_ui_updates.py` and adjust `tests/test_manager_failure.py`.

- [ ] Test that an accepted request yields exactly one replayable `sender='User'` event and one raw-history entry; a rejected busy request yields neither. Include `approve` as a separate accepted request. Test that the User message-bus echo does not add a second event.
- [ ] Run the focused tests and observe the expected failures.
- [ ] Enqueue an accepted `user_input` event before starting its worker. Process it into the event feed and raw history. Ignore User messages returning through the bus for UI and raw history. Keep agent/system/error processing unchanged.
- [ ] Run the focused tests; expect pass.

### Task 4: Browser cursor, resynchronization and duplicate-submit guard

**Files:** Modify `gui/templates/index.html`; extend `tests/test_gui_polling.js`.

- [ ] Add Node tests that two polls with the same cursor do not duplicate bubbles, a page initialized with cursor 0 renders a replayed User and Manager message, a `busy:true` chat rejection resumes polling and stays running, and a second confirmation click while a request is pending sends no second POST.
- [ ] Run `node --test tests/test_gui_polling.js`; observe expected failures.
- [ ] Remove optimistic `addMsg` calls from send actions. Add `lastEventSeq` and `pollInFlight`. Fetch `/api/updates?since=<lastEventSeq>`, process events once, then advance the cursor. Ignore stale generations. On each response, set UI running state from `data.running`; update status after that. On `busy:true`, resume polling; on other rejection, show an error and resynchronize. Disable confirmation controls immediately during a submission and prevent keyboard resubmission.
- [ ] Run Node tests; expect pass. Ensure existing tests for successful confirmation, late Manager response, and ordinary rejection still pass.

### Task 5: Regression and handoff

**Files:** Review only `gui/app.py`, `gui/templates/index.html`, `tests/test_ui_updates.py`, `tests/test_gui_polling.js`, `tests/test_manager_failure.py`.

- [ ] Run `D:\miniconda3\envs\ursa-up\python.exe -m unittest discover -s tests -v`, `node --test tests/test_gui_polling.js`, and Python `py_compile` for changed Python files. Expect all tests to pass without network calls.
- [ ] Inspect the changed branches and ensure no model, Agent, framework or configuration change is bundled with this UI work.
- [ ] Tell the user that the currently running Flask process must be restarted to load Python changes; a new browser tab or refresh alone does not reload backend code. Preserve existing result and export files.
