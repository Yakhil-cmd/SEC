[File: 'rs/sns/governance/src/lib.rs -> rs/sns/governance/src/governance.rs'] [Function: perform_upgrade_to_next_sns_version_legacy / initiate_upgrade_if_sns_behind_target_version] Can an unprivileged governance participant, under the precondition that automatically_advance_target_version=true (rs/sns/governance/src/types.rs:491) and a malicious WASM hash is blessed by NNS SNS-W for the next SNS version, trigger an automatic SNS framework upgrade (rs/sns/governance/src/governance.rs:5567-5652) that installs the malicious WASM into the SNS governance canister itself without any SN

```python
questions = [
