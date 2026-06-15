import json
import os

from decouple import config

# todo: if scope_files is: 500 > 50, 300 > 30 , 100 > 10
MAX_REPO = 10
# todo: the path from https:///github.com/dfinity/ICRC-1
SOURCE_REPO = "sei-protocol/sei-chain"
# todo: the name of the repository
REPO_NAME = "sei-chain"
run_number = os.environ.get('GITHUB_RUN_NUMBER') or os.environ.get('CI_PIPELINE_IID', '0')


def get_cyclic_index(run_number, max_index=100):
    """Convert run number to a cyclic index between 1 and max_index"""
    return (int(run_number) - 1) % max_index + 1


def load_repository_urls():
    """Load repository URLs from repositories.json."""
    repo_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "repositories.json")
    if not os.path.exists(repo_file):
        return []

    try:
        with open(repo_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return []

    if not isinstance(data, list):
        return []

    return [url for url in data if isinstance(url, str) and url.strip()]


if run_number == "0":
    BASE_URL = f"https://deepwiki.com/{SOURCE_REPO}"
else:
    repository_urls = load_repository_urls()
    if repository_urls:
        run_index = get_cyclic_index(run_number, len(repository_urls))
        BASE_URL = repository_urls[run_index - 1]
    else:
        BASE_URL = f"https://deepwiki.com/{SOURCE_REPO}"


def validation_format(report: str) -> str:
    """
    Generate a strict bounty-style validation prompt for Rootstock/PowPeg security claims.
    """
    prompt = f"""# VALIDATION PROMPT

## Security Claim
{report}


## Rules
- Validate only the submitted claim.
- Check against the RootstockLabs Immunefi impacts in questions.py target_scopes.
- Check that referenced code is in production Java source listed in questions.py scope_files.
- Do not create a new vulnerability if the submitted claim is weak or invalid.
- Do not upgrade severity unless the provided evidence proves the higher scoped impact.
- Reject leaked-key, privileged-operator, admin-only, configuration-only, phishing/social-engineering, DDoS/brute-force, docs/style, best-practice, and purely theoretical issues.
- Reject if the exploit requires unrealistic assumptions, victim mistakes, public mainnet/testnet testing, missing external context, or unsupported Rootstock/PowPeg behavior.
- A valid remote report must be triggerable indirectly through consensus-valid blockchain, Bridge, Bitcoin, RPC, bitcoind, or HSM-protocol data reachable by powpeg-node.
- Local or physical assumptions are acceptable only for scopes that explicitly say local or physical.
- The final impact must match an in-scope RootstockLabs bounty impact, not just a generic code bug.
- Prefer #NoVulnerability over speculative reports.

## Required Validation Checks
All must pass:
1. Exact in-scope file, class, method, and line/code references.
2. Clear root cause and broken Rootstock/PowPeg/HSM/Bitcoin security assumption.
3. Reachable exploit path: preconditions -> attacker action/input -> trigger -> bad result.
4. Existing checks/guards reviewed and shown insufficient: release requirements, network/chain-id binding, federation/key-id binding, sighash/value/recipient/UTXO binding, confirmations/reorg handling, HSM response validation, parser bounds, cache replay protection, fee/gas limits, exception handling, and fail-closed behavior.
5. Concrete in-scope impact with realistic likelihood.
6. Reproducible proof path: Java unit/integration test, mocked RSK/Bitcoin/HSM clients, local regtest/fork test, property/fuzz test, invariant test, or exact manual local steps.
7. No obvious rejection reason from known out-of-scope rules, privileges, brute force, or unsupported deployment assumptions.

## Silent Triage Questions
Before output, internally answer:
- Does the attacker model match the exact scope: remote, local, or physical?
- Can the input realistically reach powpeg-node under default/intended production deployment?
- Is the powpeg-node target file a necessary cause of the impact?
- Is the impact caused by this protocol code, not by an external dependency alone?
- Is bridge fund loss/theft, HSM compromise, node crash, network disruption, fee manipulation, or resource impact concrete rather than hypothetical?
- Would an Immunefi triager accept the evidence?
- What exact local test would prove it?

## Output
If valid, output exactly:

Audit Report

## Title
[Clear vulnerability statement] - ([File: file_path])

## Summary
[2-3 sentence summary of the bug and impact]

## Finding Description
[Exact code path, root cause, exploit flow, and why existing checks fail]

## Impact Explanation
[Concrete in-scope impact and severity rationale]

## Likelihood Explanation
[Attacker capability, required conditions, feasibility, repeatability]

## Recommendation
[Specific fix guidance]

## Proof of Concept
[Minimal reproducible steps or fuzz/invariant test plan]

If invalid, output exactly:
#NoVulnerability found for this question.

Output only one of the two outcomes above. No extra text.
"""
    return prompt

def audit_format(question: str) -> str:
    """
    Generate a Sei question-driven vulnerability validation prompt.
    """
    prompt = f"""# SEI QUESTION SCAN PROMPT

## Audit Question
{question}

## Rules
- This is not a vulnerability report. It is only an audit lead.
- Do not assume the question is true.
- Verify everything against the actual sei-chain code.
- Do not claim files are missing or ask for repo contents.
- Ignore tests, docs, scripts, mocks, generated code, metadata, examples, benchmarks.
- Do not report Giga-related issues.
- Do not report StateSync trusted-peer issues.
- Do not report admin, governance, validator-key, operator, leaked-key, or bad-configuration issues.
- Reject issues that require the attacker to spend funds or depend on economic griefing only.
- Always return either a valid report or exactly: #NoVulnerability found for this question.

## Scope
Valid only if it matches Sei bounty impact:
- Critical: fund loss/freeze >= $5k, unauthorized transfer/mint/burn >= $5k, hard fork needed for frozen funds.
- High: halt/crash >=1/3 validators, permanent chain split needing hard fork, default RPC crash via propagated malicious block/tx.
- Medium: proposer freeze >=10min, unintended contract execution from network bug, unauth RPC/gRPC crash, halt/crash >=10% validators, block delay >2.5s, fund loss/freeze < $5k.
- Low: wrong fee calculation, wrong mempool ordering/inclusion, halt/crash <10% validators.

## Task
Check whether the audit question leads to a real Sei vulnerability.
Only unprivileged attacker paths count.
The target file/function in the question is a starting point, not proof.
A valid answer must prove:
1. Real root cause in production code.
2. Unprivileged attacker-controlled entrypoint.
3. Feasible exploit path.
4. Real scoped impact.
5. No mitigation elsewhere.
6. Not excluded by scope.

## Check
1. Classify the bug class:
   parsing, auth/origin, ante/signature, EVM/Cosmos address, precompile/module bypass, parallel/state divergence, mempool/nonce, consensus/liveness, block validation, staking/slashing, bank/supply, IBC/bridge, wasm/gas, EVM gas/fee, replay/cache, RPC/P2P crash, resource DoS, state corruption.
2. Map to exact file/function/lines.
3. Trace attacker input from public entrypoint to vulnerable code.
4. Prove the state transition, crash, delay, fee error, fund loss, or fund freeze.
5. Check whether another validation layer rejects or mitigates it.
6. Reject if impact is only theoretical, informational, style/gas-only, or requires trusted-role mistakes.

## Reject
Reject if any are true:
- No attacker path.
- Needs validator key, governance, admin, operator, leaked key, private infra, or bad config.
- Needs malicious StateSync peer or P2P-mode state sync.
- Involves Giga, GIGA flags, or Giga-only routing.
- Only external app/contract/dependency cause.
- Only tests/docs/config/scripts/mocks/generated code.
- Theoretical only.
- Market/oracle/liquidity-only.
- Out of bounty scope.
- Missing impact or likelihood proof.

## Output
If valid:

### Title
[Clear vulnerability statement] - ([File: file_path])

### Summary
### Finding Description
### Impact Explanation
### Likelihood Explanation
### Recommendation
### Proof of Concept

Else exactly:
#NoVulnerability found for this question.
"""
    return prompt



def scan_format(report: str) -> str:
    """
    Generate a short cross-project analog scan prompt for Sei sei-chain.
    """
    prompt = f"""# ANALOG SCAN PROMPT

## External Report
{report}

## Rules
- Do not claim files are missing or ask for repo contents.
- Ignore tests, docs, configs, scripts, mocks, generated code, metadata.
- Always return either a valid report or #NoVulnerability found for this question.

## Scope
Valid only if it matches Sei bounty impact:
- Critical: fund loss/freeze >= $5k, unauthorized transfer/mint/burn >= $5k, hard fork needed for frozen funds.
- High: halt/crash >=1/3 validators, permanent chain split needing hard fork, default RPC crash via propagated malicious block/tx.
- Medium: proposer freeze >=10min, unintended contract execution from network bug, unauth RPC/gRPC crash, halt/crash >=10% validators, block delay >2.5s, fund loss/freeze < $5k.
- Low: wrong fee calculation, wrong mempool ordering/inclusion, halt/crash <10% validators.

## Task
Check if the same vuln class can exist in Sei.
External report is only a hint.
Only unprivileged attacker paths count.
Reject unless sei-chain is the necessary vulnerable step.

## Check
1. Classify: parsing, auth/origin, ante/signature, EVM/Cosmos address, precompile/module bypass, parallel/state divergence, mempool/nonce, consensus/liveness, block validation, staking/slashing, bank/supply, oracle, IBC/bridge, wasm/gas, EVM gas/fee, replay/cache, upgrade/version, RPC/P2P crash, resource DoS, state corruption.
2. Map to exact file/function/lines.
3. Prove attacker-controlled path, root cause, scoped impact, and likelihood.

## Reject
No attacker path; needs validator key/governance/admin/operator/leaked key/private infra; needs malicious StateSync peer/P2P-mode state sync; only external app/contract/dependency cause; test/docs/config/script issue; theoretical only; market/oracle/liquidity-only; out of scope; missing impact/likelihood.

## Output
If valid:

### Title
[Clear vulnerability statement] - ([File: file_path])

### Summary
### Finding Description
### Impact Explanation
### Likelihood Explanation
### Recommendation
### Proof of Concept

Else exactly:
#NoVulnerability found for this question.
"""
    return prompt
