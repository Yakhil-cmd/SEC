[File: 'engine-precompiles/src/bls12_381/g2_msm.rs -> Scope: High. Theft of unclaimed yield'] [Function: BlsG2Msm::run / engine-sdk contract::g2_msm] Can an unprivileged EVM caller supply a G2 MSM input where some pairs have valid G2 points but scalars with the high bit set (scalar >= curve order r), causing the NEAR host bls12381_g2_multiexp to perform a reduction modulo r internally

```python
questions = [
