Looking at the external report's vulnerability class — **missing self-referential address validation leading to permanent fund locking** — I need to find an IC analog where a canister's own address can be used as a parameter in a way that permanently orphans funds.

The ckBTC minter already has an explicit guard against this pattern. Let me verify whether the ckETH minter does too.