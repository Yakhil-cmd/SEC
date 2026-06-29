[File: 'File Name: etc/eth-contracts/contracts/AdminControlled.sol -> Scope: Critical. Insolvency'] [Function: adminSstore / EvmErc20 _decimals slot (slot 9) + withdrawToNear amount encoding] Can an attacker-controlled admin call adminSstore(9, 0) to set _decimals to 0 under the precondition that off-chain accounting and NEAR-side NEP-141 metadata use a different decimal precision, triggering a metadata desync where the EVM token reports 0 decimals while the NEAR-side NEP-141 token retains its original decimals, violating the invariant that EVM and NEAR-side token metadata must remain consistent

```python
questions = [
