# Runbook (later run on t2-dev). Nothing here has been executed.

Do not start until PREREGISTRATION.md is frozen (hash in the ticket) and the user's answers are in. t2-dev also hosts GoodQA staging and is at about 93% disk: cap everything, bind localhost only, check `df -h` first and abort if free space is below the cap plus margin. Placeholders in angle brackets are filled at run time; no secret is ever typed on a command line or placed in an environment variable.

## 0. Preconditions
```
ssh -F ~/.ssh/config_fleetops t2-dev 'df -h / ; free -m ; nproc'
```
Record the output in the run manifest. Abort if the disk budget below does not fit.

## 1. Elasticsearch 9.5.x (single node, localhost only, capped)
Container or tarball per the user's decision; either way:
- bind `127.0.0.1:<ES_PORT>` only; no published ports on other interfaces
- heap `-Xms<HEAP>g -Xmx<HEAP>g`, CPU cap `<CPUS>`, memory cap `<MEM>g`, data path on a volume capped at `<DISK>g`
- security enabled; the API key is created on the node and written to `<CRED_FILE>` (mode 0600, owned by the run user, outside the repo). It is never echoed.
```
chmod 600 <CRED_FILE>
```

## 2. Reach the node from the runner
Run the runner on t2-dev itself, or tunnel (the client refuses non-loopback hosts):
```
ssh -F ~/.ssh/config_fleetops -N -L <LOCAL_PORT>:127.0.0.1:<ES_PORT> t2-dev
```

## 3. Environment (versions pinned in eval/requirements.txt; installed only now, in a venv under the work directory)
```
python3 -m venv <WORK>/venv && <WORK>/venv/bin/pip install -r eval/requirements.txt
```
Fetch the dataset into its own empty directory (`<DATA_DIR>`) and record its checksum and license in the manifest. The dataset and model downloads are the steps that need the user's go-ahead.

## 4. Sanity (no dataset)
```
cd eval && python3 -m unittest
cd eval && python3 -m emlab.runner --dry-run --out <WORK>/dry.json
```

## 5. Phase R (live runner commands are not implemented in the scaffold; this is the intended interface)
```
cd eval && python3 -m emlab.runner --phase R --es-url http://127.0.0.1:<ES_PORT> --credential-file <CRED_FILE> --dataset-path <DATA_DIR>/<FILE> --split pilot --seed 16096 --out <WORK>/phaseR_pilot.json
cd eval && python3 -m emlab.runner --phase R --es-url http://127.0.0.1:<ES_PORT> --credential-file <CRED_FILE> --dataset-path <DATA_DIR>/<FILE> --split test --seed 16096 --out <WORK>/phaseR_test.json
```
Pilot first; fix the configuration and Phase F thresholds from the pilot, commit them, then run test once.

## 6. Phase F
Same interface with `--phase F`, model identifiers from the frozen pre-registration; provider credentials only via a credential FILE path, never an environment variable or argument.

## 7. Teardown
Stop and remove the Elasticsearch container or process, delete its data volume, stop the tunnel, confirm disk is back to the recorded baseline, and keep outputs under `<WORK>`. Record each step in the WAL.
