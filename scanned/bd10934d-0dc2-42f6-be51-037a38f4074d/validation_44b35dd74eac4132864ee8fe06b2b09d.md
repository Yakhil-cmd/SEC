### Title
Integer Division Precision Loss in ICP/XDR Rate-Based Maturity Modulation Calculation - (File: rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs)

### Summary
The `compute_maturity_modulation_permyriad` function in the NNS Governance canister and `compute_capped_maturity_modulation` in the Cycles Minting Canister both perform integer division without sufficient decimal pre-scaling. When the ICP/XDR price difference between averaging windows is small relative to the divisor, the result silently truncates to zero, producing a systematically wrong maturity modulation that is applied to every NNS neuron maturity disbursement.

### Finding Description
In `rs/nns/governance/src/timer_tasks/update_icp_xdr_rate_related_data.rs`, `compute_maturity_modulation_permyriad