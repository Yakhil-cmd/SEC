### Title
Unbounded EVM Gas in CosmWasm Query Path via `StaticCallEVM` - (File: `x/evm/client/wasm/query.go`)

---

### Summary

The GRPC `StaticCall` query endpoint was patched to cap EVM gas when the context uses an infinite gas meter. However, the CosmWasm query path — all ~20 handlers in `x/evm/client/wasm/query.go` — was not patched. Every handler calls `k.StaticCallEVM` directly with the raw CosmWasm context, which carries an infinite gas meter (`Limit() == 0`). This causes `getEvmGasLimitFromCtx` to return `math.MaxUint64` as the EVM gas limit, allowing unbounded EVM execution during block processing.

---

### Finding Description

`getEvmGasLimitFromCtx` contains the root cause:

```go
func (k *Keeper) getEvmGasLimitFromCtx(ctx sdk.Context) uint64 {
    seiGasRemaining := ctx.GasMeter().Limit() - ctx.GasMeter().GasConsumedToLimit()
    if ctx.GasMeter().Limit() <= 0 {
        return math.MaxUint64   // ← unbounded when limit is 0
    }
    ...
}
``` [1](#0-0) 

The GRPC handler was patched to guard against this:

```go
if ctx.GasMeter().Limit() == 0 {
    ctx = ctx.WithGasMeter(sdk.NewGasMeterWithMultiplier(ctx, q.QueryConfig.GasLimit))
}
``` [2](#0-1) 

The default cap is 300,000 EVM gas. [3](#0-2) 

But **none** of the CosmWasm handlers apply this guard. `HandleStaticCall` is representative:

```go
func (h *EVMQueryHandler) HandleStaticCall(ctx sdk.Context, from string, to string, data []byte) ([]byte, error) {
    ...
    res, err := h.k.StaticCallEVM(ctx, fromAddr, toAddr, data)  // ctx has infinite gas meter
    ...
}
``` [4](#0-3) 

The same pattern applies to every other handler in the file: `HandleERC20TokenInfo`, `HandleERC20Balance`, `HandleERC20Allowance`, `HandleERC721Owner`, `HandleERC721Approved`, `HandleERC721IsApprovedForAll`, `HandleERC721TotalSupply`, `HandleERC721NameSymbol`, `HandleERC721Uri`, `HandleERC721RoyaltyInfo`, `HandleERC1155IsApprovedForAll`, `HandleERC1155BalanceOf`, `HandleERC1155BalanceOfBatch`, `HandleERC1155Uri`, `HandleERC1155TotalSupply`, `HandleERC1155TotalSupplyForToken`, `HandleERC1155TokenExists`, `HandleERC1155NameSymbol`, `HandleERC1155RoyaltyInfo`, `HandleSupportsInterface`. [5](#0-4) 

These handlers are wired into the CosmWasm query plugin, which is invoked during CosmWasm contract execution — i.e., **during block processing**, not just at the RPC layer. [6](#0-5) 

`StaticCallEVM` → `callEVM` → `getEvmGasLimitFromCtx` returns `math.MaxUint64` → `evm.StaticCall(caller, *addr, input, math.MaxUint64)`. [7](#0-6) 

---

### Impact Explanation

Because CosmWasm contract execution happens during block processing, an attacker can force every validator to spend unbounded CPU time executing EVM bytecode. This can:

- Delay block production beyond the consensus timeout, causing missed blocks.
- Cause block processing to stall if the computation is long enough, potentially halting the chain.

This maps to **Medium** (block delay > 2.5s, unintended contract execution from network bug) or **High** (halt/crash ≥ 1/3 validators) depending on the severity of the computation deployed.

---

### Likelihood Explanation

The attack requires only:
1. Deploying a computationally expensive EVM contract (permissionless).
2. Deploying a CosmWasm contract that calls `static_call` (or any ERC20/ERC721/ERC1155 query binding) targeting the expensive contract (permissionless).
3. Executing a transaction that invokes the CosmWasm contract.

No privileged access, governance, or validator keys are needed. The attack is fully unprivileged and repeatable every block.

---

### Recommendation

Apply the same gas cap used in the GRPC handler to all CosmWasm query handlers. The simplest fix is to check and replace the gas meter at the top of each handler (or in a shared wrapper):

```go
if ctx.GasMeter().Limit() == 0 {
    ctx = ctx.WithGasMeter(sdk.NewGasMeterWithMultiplier(ctx, queryGasLimit))
}
```

Alternatively, apply the cap inside `StaticCallEVM` itself so all callers benefit automatically.

---

### Proof of Concept

1. Deploy an EVM contract with a tight infinite loop bounded only by gas (e.g., a loop that runs ~`math.MaxUint64 / opcode_cost` iterations).
2. Deploy a CosmWasm contract whose `query` handler issues:
   ```json
   { "static_call": { "from": "<addr>", "to": "<expensive_evm_contract>", "data": "<call_data>" } }
   ```
3. Submit a transaction that calls the CosmWasm contract.
4. Every validator processing this block will invoke `HandleStaticCall` → `StaticCallEVM` with `gas = math.MaxUint64`, executing the EVM loop without bound, stalling block finalization. [8](#0-7) [4](#0-3) [9](#0-8)

### Citations

**File:** x/evm/keeper/evm.go (L157-179)
```go
func (k *Keeper) StaticCallEVM(ctx sdk.Context, from sdk.AccAddress, to *common.Address, data []byte) ([]byte, error) {
	evm, err := k.createReadOnlyEVM(ctx, from)
	if err != nil {
		return nil, err
	}
	return k.callEVM(ctx, k.GetEVMAddressOrDefault(ctx, from), to, nil, data, func(caller common.Address, addr *common.Address, input []byte, gas uint64, _ *big.Int) ([]byte, uint64, error) {
		return evm.StaticCall(caller, *addr, input, gas)
	})
}

func (k *Keeper) callEVM(ctx sdk.Context, from common.Address, to *common.Address, val *sdk.Int, data []byte, f EVMCallFunc) ([]byte, error) {
	evmGasLimit := k.getEvmGasLimitFromCtx(ctx)
	value := utils.Big0
	if val != nil {
		value = val.BigInt()
	}
	ret, leftoverGas, err := f(from, to, data, evmGasLimit, value)
	k.consumeEvmGas(ctx, evmGasLimit-leftoverGas)
	if err != nil {
		return nil, err
	}
	return ret, nil
}
```

**File:** x/evm/keeper/evm.go (L198-211)
```go
func (k *Keeper) getEvmGasLimitFromCtx(ctx sdk.Context) uint64 {
	seiGasRemaining := ctx.GasMeter().Limit() - ctx.GasMeter().GasConsumedToLimit()
	if ctx.GasMeter().Limit() <= 0 {
		return math.MaxUint64
	}
	if ctx.ChainID() != Pacific1ChainID || ctx.BlockHeight() >= 119821526 {
		ctx = ctx.WithGasMeter(sdk.NewInfiniteGasMeterWithMultiplier(ctx))
	}
	evmGasBig := sdk.NewDecFromInt(sdk.NewIntFromUint64(seiGasRemaining)).Quo(k.GetPriorityNormalizer(ctx)).TruncateInt().BigInt()
	if evmGasBig.Cmp(MaxUint64BigInt) > 0 {
		evmGasBig = MaxUint64BigInt
	}
	return evmGasBig.Uint64()
}
```

**File:** x/evm/keeper/grpc_query.go (L66-80)
```go
func (q Querier) StaticCall(c context.Context, req *types.QueryStaticCallRequest) (*types.QueryStaticCallResponse, error) {
	ctx := sdk.UnwrapSDKContext(c)
	if req.To == "" {
		return nil, errors.New("cannot use static call to create contracts")
	}
	if ctx.GasMeter().Limit() == 0 {
		ctx = ctx.WithGasMeter(sdk.NewGasMeterWithMultiplier(ctx, q.QueryConfig.GasLimit))
	}
	to := common.HexToAddress(req.To)
	res, err := q.StaticCallEVM(ctx, q.Keeper.AccountKeeper().GetModuleAddress(types.ModuleName), &to, req.Data)
	if err != nil {
		return nil, err
	}
	return &types.QueryStaticCallResponse{Data: res}, nil
}
```

**File:** x/evm/querier/config.go (L12-14)
```go
var DefaultConfig = Config{
	GasLimit: 300000,
}
```

**File:** x/evm/client/wasm/query.go (L29-45)
```go
func (h *EVMQueryHandler) HandleStaticCall(ctx sdk.Context, from string, to string, data []byte) ([]byte, error) {
	fromAddr, err := sdk.AccAddressFromBech32(from)
	if err != nil {
		return nil, err
	}
	var toAddr *common.Address
	if to != "" {
		toSeiAddr := common.HexToAddress(to)
		toAddr = &toSeiAddr
	}
	res, err := h.k.StaticCallEVM(ctx, fromAddr, toAddr, data)
	if err != nil {
		return nil, err
	}
	response := bindings.StaticCallResponse{EncodedData: base64.StdEncoding.EncodeToString(res)}
	return json.Marshal(response)
}
```

**File:** x/evm/client/wasm/query.go (L64-1000)
```go
func (h *EVMQueryHandler) HandleERC20TokenInfo(ctx sdk.Context, contractAddress string, caller string) ([]byte, error) {
	callerAddr, err := sdk.AccAddressFromBech32(caller)
	if err != nil {
		return nil, err
	}
	contract := common.HexToAddress(contractAddress)
	abi, err := native.NativeMetaData.GetAbi()
	if err != nil {
		return nil, err
	}
	response := bindings.ERC20TokenInfoResponse{}

	bz, err := abi.Pack("totalSupply")
	if err != nil {
		return nil, err
	}
	res, err := h.k.StaticCallEVM(ctx, callerAddr, &contract, bz)
	if err != nil {
		return nil, err
	}
	unpacked, err := abi.Unpack("totalSupply", res)
	if err != nil {
		return nil, err
	}
	totalSupply := sdk.NewIntFromBigInt(unpacked[0].(*big.Int))
	response.TotalSupply = &totalSupply

	bz, err = abi.Pack("name")
	if err != nil {
		return nil, err
	}
	res, err = h.k.StaticCallEVM(ctx, callerAddr, &contract, bz)
	if err != nil {
		return nil, err
	}
	unpacked, err = abi.Unpack("name", res)
	if err != nil {
		return nil, err
	}
	response.Name = unpacked[0].(string)

	bz, err = abi.Pack("symbol")
	if err != nil {
		return nil, err
	}
	res, err = h.k.StaticCallEVM(ctx, callerAddr, &contract, bz)
	if err != nil {
		return nil, err
	}
	unpacked, err = abi.Unpack("symbol", res)
	if err != nil {
		return nil, err
	}
	response.Symbol = unpacked[0].(string)

	bz, err = abi.Pack("decimals")
	if err != nil {
		return nil, err
	}
	res, err = h.k.StaticCallEVM(ctx, callerAddr, &contract, bz)
	if err != nil {
		return nil, err
	}
	unpacked, err = abi.Unpack("decimals", res)
	if err != nil {
		return nil, err
	}
	response.Decimals = unpacked[0].(byte)

	return json.Marshal(response)
}

func (h *EVMQueryHandler) HandleERC20Balance(ctx sdk.Context, contractAddress string, account string) ([]byte, error) {
	addr, err := sdk.AccAddressFromBech32(account)
	if err != nil {
		return nil, err
	}
	evmAddr, found := h.k.GetEVMAddress(ctx, addr)
	if !found {
		return nil, types.NewAssociationMissingErr(addr.String())
	}
	contract := common.HexToAddress(contractAddress)
	abi, err := native.NativeMetaData.GetAbi()
	if err != nil {
		return nil, err
	}

	bz, err := abi.Pack("balanceOf", evmAddr)
	if err != nil {
		return nil, err
	}
	res, err := h.k.StaticCallEVM(ctx, addr, &contract, bz)
	if err != nil {
		return nil, err
	}
	unpacked, err := abi.Unpack("balanceOf", res)
	if err != nil {
		return nil, err
	}
	balance := sdk.NewIntFromBigInt(unpacked[0].(*big.Int))
	return json.Marshal(bindings.ERC20BalanceResponse{Balance: &balance})
}

func (h *EVMQueryHandler) HandleERC721Owner(ctx sdk.Context, caller string, contractAddress string, tokenId string) ([]byte, error) {
	callerAddr, err := sdk.AccAddressFromBech32(caller)
	if err != nil {
		return nil, err
	}
	contract := common.HexToAddress(contractAddress)
	abi, err := cw721.Cw721MetaData.GetAbi()
	if err != nil {
		return nil, err
	}
	t, ok := sdk.NewIntFromString(tokenId)
	if !ok {
		return nil, errors.New("invalid token ID for ERC721, must be a big Int")
	}
	bz, err := abi.Pack("ownerOf", t.BigInt())
	if err != nil {
		return nil, err
	}
	res, err := h.k.StaticCallEVM(ctx, callerAddr, &contract, bz)
	if err != nil {
		return nil, err
	}
	typed, err := abi.Unpack("ownerOf", res)
	if err != nil {
		return nil, err
	}
	typedOwner := typed[0].(common.Address)
	owner := ""
	if (typedOwner != common.Address{}) {
		owner = h.k.GetSeiAddressOrDefault(ctx, typedOwner).String()
	}
	response := bindings.ERC721OwnerResponse{Owner: owner}
	return json.Marshal(response)
}

func (h *EVMQueryHandler) HandleERC721TransferPayload(ctx sdk.Context, from string, recipient string, tokenId string) ([]byte, error) {
	abi, err := cw721.Cw721MetaData.GetAbi()
	if err != nil {
		return nil, err
	}
	fromEvmAddr, found := h.k.GetEVMAddress(ctx, sdk.MustAccAddressFromBech32(from))
	if !found {
		return nil, types.NewAssociationMissingErr(from)
	}
	toEvmAddr, found := h.k.GetEVMAddress(ctx, sdk.MustAccAddressFromBech32(recipient))
	if !found {
		return nil, types.NewAssociationMissingErr(recipient)
	}
	t, ok := sdk.NewIntFromString(tokenId)
	if !ok {
		return nil, errors.New("invalid token ID for ERC721, must be a big Int")
	}
	bz, err := abi.Pack("transferFrom", fromEvmAddr, toEvmAddr, t.BigInt())
	if err != nil {
		return nil, err
	}
	res := bindings.ERCPayloadResponse{EncodedPayload: base64.StdEncoding.EncodeToString(bz)}
	return json.Marshal(res)
}

func (h *EVMQueryHandler) HandleERC721ApprovePayload(ctx sdk.Context, spender string, tokenId string) ([]byte, error) {
	spenderEvmAddr := common.Address{} // empty address if approval should be revoked (i.e. spender string is empty)
	var err error
	if spender != "" {
		evmAddr, found := h.k.GetEVMAddress(ctx, sdk.MustAccAddressFromBech32(spender))
		if !found {
			return nil, types.NewAssociationMissingErr(spender)
		}
		spenderEvmAddr = evmAddr
	}
	abi, err := cw721.Cw721MetaData.GetAbi()
	if err != nil {
		return nil, err
	}
	t, ok := sdk.NewIntFromString(tokenId)
	if !ok {
		return nil, errors.New("invalid token ID for ERC721, must be a big Int")
	}
	bz, err := abi.Pack("approve", spenderEvmAddr, t.BigInt())
	if err != nil {
		return nil, err
	}
	res := bindings.ERCPayloadResponse{EncodedPayload: base64.StdEncoding.EncodeToString(bz)}
	return json.Marshal(res)
}

func (h *EVMQueryHandler) HandleERC721SetApprovalAllPayload(ctx sdk.Context, to string, approved bool) ([]byte, error) {
	evmAddr, found := h.k.GetEVMAddress(ctx, sdk.MustAccAddressFromBech32(to))
	if !found {
		return nil, types.NewAssociationMissingErr(to)
	}
	abi, err := cw721.Cw721MetaData.GetAbi()
	if err != nil {
		return nil, err
	}
	bz, err := abi.Pack("setApprovalForAll", evmAddr, approved)
	if err != nil {
		return nil, err
	}
	res := bindings.ERCPayloadResponse{EncodedPayload: base64.StdEncoding.EncodeToString(bz)}
	return json.Marshal(res)
}

func (h *EVMQueryHandler) HandleERC20TransferFromPayload(ctx sdk.Context, owner string, recipient string, amount *sdk.Int) ([]byte, error) {
	abi, err := native.NativeMetaData.GetAbi()
	if err != nil {
		return nil, err
	}
	ownerEvmAddr, found := h.k.GetEVMAddress(ctx, sdk.MustAccAddressFromBech32(owner))
	if !found {
		return nil, types.NewAssociationMissingErr(owner)
	}
	recipientEvmAddr, found := h.k.GetEVMAddress(ctx, sdk.MustAccAddressFromBech32(recipient))
	if !found {
		return nil, types.NewAssociationMissingErr(recipient)
	}
	bz, err := abi.Pack("transferFrom", ownerEvmAddr, recipientEvmAddr, amount.BigInt())
	if err != nil {
		return nil, err
	}
	res := bindings.ERCPayloadResponse{EncodedPayload: base64.StdEncoding.EncodeToString(bz)}
	return json.Marshal(res)
}

func (h *EVMQueryHandler) HandleERC20ApprovePayload(ctx sdk.Context, spender string, amount *sdk.Int) ([]byte, error) {
	abi, err := native.NativeMetaData.GetAbi()
	if err != nil {
		return nil, err
	}
	spenderEvmAddr, found := h.k.GetEVMAddress(ctx, sdk.MustAccAddressFromBech32(spender))
	if !found {
		return nil, types.NewAssociationMissingErr(spender)
	}

	bz, err := abi.Pack("approve", spenderEvmAddr, amount.BigInt())
	if err != nil {
		return nil, err
	}
	res := bindings.ERCPayloadResponse{EncodedPayload: base64.StdEncoding.EncodeToString(bz)}
	return json.Marshal(res)
}

func (h *EVMQueryHandler) HandleERC20Allowance(ctx sdk.Context, contractAddress string, owner string, spender string) ([]byte, error) {
	// Get the evm address of the owner
	ownerAddr, err := sdk.AccAddressFromBech32(owner)
	if err != nil {
		return nil, err
	}
	ownerEvmAddr, found := h.k.GetEVMAddress(ctx, ownerAddr)
	if !found {
		return nil, types.NewAssociationMissingErr(ownerAddr.String())
	}

	// Get the evm address of spender
	spenderEvmAddr, found := h.k.GetEVMAddress(ctx, sdk.MustAccAddressFromBech32(spender))
	if !found {
		return nil, types.NewAssociationMissingErr(spender)
	}

	// Fetch the contract ABI
	contract := common.HexToAddress(contractAddress)
	abi, err := native.NativeMetaData.GetAbi()
	if err != nil {
		return nil, err
	}

	// Make the query to allowance(owner, spender)
	bz, err := abi.Pack("allowance", ownerEvmAddr, spenderEvmAddr)
	if err != nil {
		return nil, err
	}
	res, err := h.k.StaticCallEVM(ctx, ownerAddr, &contract, bz)
	if err != nil {
		return nil, err
	}

	// Parse the response (Should be of type uint256 if successful)
	typed, err := abi.Unpack("allowance", res)
	if err != nil {
		return nil, err
	}
	allowance := typed[0].(*big.Int)
	allowanceSdk := sdk.NewIntFromBigInt(allowance)
	response := bindings.ERC20AllowanceResponse{Allowance: &allowanceSdk}
	return json.Marshal(response)
}

func (h *EVMQueryHandler) HandleERC721Approved(ctx sdk.Context, caller string, contractAddress string, tokenId string) ([]byte, error) {
	callerAddr, err := sdk.AccAddressFromBech32(caller)
	if err != nil {
		return nil, err
	}
	contract := common.HexToAddress(contractAddress)
	abi, err := cw721.Cw721MetaData.GetAbi()
	if err != nil {
		return nil, err
	}
	t, ok := sdk.NewIntFromString(tokenId)
	if !ok {
		return nil, errors.New("invalid token ID for ERC721, must be a big Int")
	}
	bz, err := abi.Pack("getApproved", t.BigInt())
	if err != nil {
		return nil, err
	}
	res, err := h.k.StaticCallEVM(ctx, callerAddr, &contract, bz)
	if err != nil {
		return nil, err
	}
	typed, err := abi.Unpack("getApproved", res)
	if err != nil {
		return nil, err
	}
	approved := typed[0].(common.Address)
	a := ""
	if (approved != common.Address{}) {
		a = h.k.GetSeiAddressOrDefault(ctx, approved).String()
	}
	response := bindings.ERC721ApprovedResponse{Approved: a}
	return json.Marshal(response)
}

func (h *EVMQueryHandler) HandleERC721IsApprovedForAll(ctx sdk.Context, caller string, contractAddress string, owner string, operator string) ([]byte, error) {
	callerAddr, err := sdk.AccAddressFromBech32(caller)
	if err != nil {
		return nil, err
	}
	ownerEvmAddr, found := h.k.GetEVMAddress(ctx, sdk.MustAccAddressFromBech32(owner))
	if !found {
		return nil, types.NewAssociationMissingErr(owner)
	}
	operatorEvmAddr, found := h.k.GetEVMAddress(ctx, sdk.MustAccAddressFromBech32(operator))
	if !found {
		return nil, types.NewAssociationMissingErr(operator)
	}
	contract := common.HexToAddress(contractAddress)
	abi, err := cw721.Cw721MetaData.GetAbi()
	if err != nil {
		return nil, err
	}
	bz, err := abi.Pack("isApprovedForAll", ownerEvmAddr, operatorEvmAddr)
	if err != nil {
		return nil, err
	}
	res, err := h.k.StaticCallEVM(ctx, callerAddr, &contract, bz)
	if err != nil {
		return nil, err
	}
	typed, err := abi.Unpack("isApprovedForAll", res)
	if err != nil {
		return nil, err
	}
	response := bindings.ERC721IsApprovedForAllResponse{IsApproved: typed[0].(bool)}
	return json.Marshal(response)
}

func (h *EVMQueryHandler) HandleERC721TotalSupply(ctx sdk.Context, caller string, contractAddress string) ([]byte, error) {
	callerAddr, err := sdk.AccAddressFromBech32(caller)
	if err != nil {
		return nil, err
	}
	contract := common.HexToAddress(contractAddress)
	abi, err := cw721.Cw721MetaData.GetAbi()
	if err != nil {
		return nil, err
	}
	bz, err := abi.Pack("totalSupply")
	if err != nil {
		return nil, err
	}
	res, err := h.k.StaticCallEVM(ctx, callerAddr, &contract, bz)
	if err != nil {
		return nil, err
	}
	typed, err := abi.Unpack("totalSupply", res)
	if err != nil {
		return nil, err
	}
	totalSupply := sdk.NewIntFromBigInt(typed[0].(*big.Int))
	response := bindings.ERC721TotalSupplyResponse{Supply: &totalSupply}
	return json.Marshal(response)
}

func (h *EVMQueryHandler) HandleERC721NameSymbol(ctx sdk.Context, caller string, contractAddress string) ([]byte, error) {
	callerAddr, err := sdk.AccAddressFromBech32(caller)
	if err != nil {
		return nil, err
	}
	contract := common.HexToAddress(contractAddress)
	abi, err := cw721.Cw721MetaData.GetAbi()
	if err != nil {
		return nil, err
	}
	bz, err := abi.Pack("name")
	if err != nil {
		return nil, err
	}
	res, err := h.k.StaticCallEVM(ctx, callerAddr, &contract, bz)
	if err != nil {
		return nil, err
	}
	typed, err := abi.Unpack("name", res)
	if err != nil {
		return nil, err
	}
	name := typed[0].(string)
	bz, err = abi.Pack("symbol")
	if err != nil {
		return nil, err
	}
	res, err = h.k.StaticCallEVM(ctx, callerAddr, &contract, bz)
	if err != nil {
		return nil, err
	}
	typed, err = abi.Unpack("symbol", res)
	if err != nil {
		return nil, err
	}
	symbol := typed[0].(string)
	response := bindings.ERC721NameSymbolResponse{Name: name, Symbol: symbol}
	return json.Marshal(response)
}

func (h *EVMQueryHandler) HandleERC721Uri(ctx sdk.Context, caller string, contractAddress string, tokenId string) ([]byte, error) {
	callerAddr, err := sdk.AccAddressFromBech32(caller)
	if err != nil {
		return nil, err
	}
	t, ok := sdk.NewIntFromString(tokenId)
	if !ok {
		return nil, errors.New("invalid token ID for ERC721, must be a big Int")
	}
	contract := common.HexToAddress(contractAddress)
	abi, err := cw721.Cw721MetaData.GetAbi()
	if err != nil {
		return nil, err
	}
	bz, err := abi.Pack("tokenURI", t.BigInt())
	if err != nil {
		return nil, err
	}
	res, err := h.k.StaticCallEVM(ctx, callerAddr, &contract, bz)
	if err != nil {
		return nil, err
	}
	typed, err := abi.Unpack("tokenURI", res)
	if err != nil {
		return nil, err
	}
	response := bindings.ERC721UriResponse{Uri: typed[0].(string)}
	return json.Marshal(response)
}

func (h *EVMQueryHandler) HandleERC721RoyaltyInfo(ctx sdk.Context, caller string, contractAddress string, tokenId string, salePrice *sdk.Int) ([]byte, error) {
	callerAddr, err := sdk.AccAddressFromBech32(caller)
	if err != nil {
		return nil, err
	}
	t, ok := sdk.NewIntFromString(tokenId)
	if !ok {
		return nil, errors.New("invalid token ID for ERC721, must be a big Int")
	}
	contract := common.HexToAddress(contractAddress)
	abi, err := cw721.Cw721MetaData.GetAbi()
	if err != nil {
		return nil, err
	}
	bz, err := abi.Pack("royaltyInfo", t.BigInt(), salePrice.BigInt())
	if err != nil {
		return nil, err
	}
	res, err := h.k.StaticCallEVM(ctx, callerAddr, &contract, bz)
	if err != nil {
		return nil, err
	}
	typed, err := abi.Unpack("royaltyInfo", res)
	if err != nil {
		return nil, err
	}

	typedReceiver := typed[0].(common.Address)
	receiver := ""
	if (typedReceiver != common.Address{}) {
		receiver = h.k.GetSeiAddressOrDefault(ctx, typedReceiver).String()
	}
	royaltyAmount := sdk.NewIntFromBigInt(typed[1].(*big.Int))
	response := bindings.ERC721RoyaltyInfoResponse{Receiver: receiver, RoyaltyAmount: &royaltyAmount}
	return json.Marshal(response)
}

func (h *EVMQueryHandler) HandleERC1155TransferPayload(ctx sdk.Context, from string, recipient string, tokenId string, amount *sdk.Int) ([]byte, error) {
	abi, err := cw1155.Cw1155MetaData.GetAbi()
	if err != nil {
		return nil, err
	}
	fromEvmAddr, found := h.k.GetEVMAddress(ctx, sdk.MustAccAddressFromBech32(from))
	if !found {
		return nil, types.NewAssociationMissingErr(from)
	}
	toEvmAddr, found := h.k.GetEVMAddress(ctx, sdk.MustAccAddressFromBech32(recipient))
	if !found {
		return nil, types.NewAssociationMissingErr(recipient)
	}
	t, ok := sdk.NewIntFromString(tokenId)
	if !ok {
		return nil, errors.New("invalid token ID for ERC721, must be a big Int")
	}
	bz, err := abi.Pack("safeTransferFrom", fromEvmAddr, toEvmAddr, t.BigInt(), amount.BigInt(), []byte("0x0"))
	if err != nil {
		return nil, err
	}
	res := bindings.ERCPayloadResponse{EncodedPayload: base64.StdEncoding.EncodeToString(bz)}
	return json.Marshal(res)
}

func (h *EVMQueryHandler) HandleERC1155BatchTransferPayload(ctx sdk.Context, from string, recipient string, tokenIds []string, amounts []*sdk.Int) ([]byte, error) {
	abi, err := cw1155.Cw1155MetaData.GetAbi()
	if err != nil {
		return nil, err
	}
	if len(tokenIds) != len(amounts) {
		return nil, errors.New("mismatched argument lengths for tokenIds and amounts")
	}
	fromEvmAddr, found := h.k.GetEVMAddress(ctx, sdk.MustAccAddressFromBech32(from))
	if !found {
		return nil, types.NewAssociationMissingErr(from)
	}
	toEvmAddr, found := h.k.GetEVMAddress(ctx, sdk.MustAccAddressFromBech32(recipient))
	if !found {
		return nil, types.NewAssociationMissingErr(recipient)
	}
	var tIds []*big.Int
	for i := 0; i < len(tokenIds); i++ {
		t, ok := sdk.NewIntFromString(tokenIds[i])
		if !ok {
			return nil, errors.New("invalid token ID for ERC1155, must be a big Int")
		}
		tIds = append(tIds, t.BigInt())
	}
	var tAmounts []*big.Int
	for i := 0; i < len(amounts); i++ {
		tAmounts = append(tAmounts, amounts[i].BigInt())
	}
	bz, err := abi.Pack("safeBatchTransferFrom", fromEvmAddr, toEvmAddr, tIds, tAmounts, []byte("0x0"))
	if err != nil {
		return nil, err
	}
	res := bindings.ERCPayloadResponse{EncodedPayload: base64.StdEncoding.EncodeToString(bz)}
	return json.Marshal(res)
}

func (h *EVMQueryHandler) HandleERC1155SetApprovalAllPayload(ctx sdk.Context, to string, approved bool) ([]byte, error) {
	evmAddr, found := h.k.GetEVMAddress(ctx, sdk.MustAccAddressFromBech32(to))
	if !found {
		return nil, types.NewAssociationMissingErr(to)
	}
	abi, err := cw1155.Cw1155MetaData.GetAbi()
	if err != nil {
		return nil, err
	}
	bz, err := abi.Pack("setApprovalForAll", evmAddr, approved)
	if err != nil {
		return nil, err
	}
	res := bindings.ERCPayloadResponse{EncodedPayload: base64.StdEncoding.EncodeToString(bz)}
	return json.Marshal(res)
}

func (h *EVMQueryHandler) HandleERC1155IsApprovedForAll(ctx sdk.Context, caller string, contractAddress string, owner string, operator string) ([]byte, error) {
	callerAddr, err := sdk.AccAddressFromBech32(caller)
	if err != nil {
		return nil, err
	}
	ownerEvmAddr, found := h.k.GetEVMAddress(ctx, sdk.MustAccAddressFromBech32(owner))
	if !found {
		return nil, types.NewAssociationMissingErr(owner)
	}
	operatorEvmAddr, found := h.k.GetEVMAddress(ctx, sdk.MustAccAddressFromBech32(operator))
	if !found {
		return nil, types.NewAssociationMissingErr(operator)
	}
	contract := common.HexToAddress(contractAddress)
	abi, err := cw1155.Cw1155MetaData.GetAbi()
	if err != nil {
		return nil, err
	}
	bz, err := abi.Pack("isApprovedForAll", ownerEvmAddr, operatorEvmAddr)
	if err != nil {
		return nil, err
	}
	res, err := h.k.StaticCallEVM(ctx, callerAddr, &contract, bz)
	if err != nil {
		return nil, err
	}
	typed, err := abi.Unpack("isApprovedForAll", res)
	if err != nil {
		return nil, err
	}
	response := bindings.ERC1155IsApprovedForAllResponse{IsApproved: typed[0].(bool)}
	return json.Marshal(response)
}

func (h *EVMQueryHandler) HandleERC1155BalanceOf(ctx sdk.Context, caller string, contractAddress string, account string, tokenId string) ([]byte, error) {
	callerAddr, err := sdk.AccAddressFromBech32(caller)
	if err != nil {
		return nil, err
	}
	abi, err := cw1155.Cw1155MetaData.GetAbi()
	if err != nil {
		return nil, err
	}
	addr, err := sdk.AccAddressFromBech32(account)
	if err != nil {
		return nil, err
	}
	evmAddr, found := h.k.GetEVMAddress(ctx, addr)
	if !found {
		return nil, types.NewAssociationMissingErr(addr.String())
	}
	t, ok := sdk.NewIntFromString(tokenId)
	if !ok {
		return nil, errors.New("invalid token ID for ERC1155, must be a big Int")
	}

	contract := common.HexToAddress(contractAddress)
	bz, err := abi.Pack("balanceOf", evmAddr, t.BigInt())
	if err != nil {
		return nil, err
	}
	res, err := h.k.StaticCallEVM(ctx, callerAddr, &contract, bz)
	if err != nil {
		return nil, err
	}
	unpacked, err := abi.Unpack("balanceOf", res)
	if err != nil {
		return nil, err
	}
	balance := sdk.NewIntFromBigInt(unpacked[0].(*big.Int))
	return json.Marshal(bindings.ERC1155BalanceOfResponse{Balance: &balance})
}

func (h *EVMQueryHandler) HandleERC1155BalanceOfBatch(ctx sdk.Context, caller string, contractAddress string, accounts []string, tokenIds []string) ([]byte, error) {
	if len(accounts) != len(tokenIds) {
		return nil, errors.New("mismatched argument lengths for accounts and tokenIds")
	}
	callerAddr, err := sdk.AccAddressFromBech32(caller)
	if err != nil {
		return nil, err
	}
	abi, err := cw1155.Cw1155MetaData.GetAbi()
	if err != nil {
		return nil, err
	}
	contract := common.HexToAddress(contractAddress)

	var evmAddrs []common.Address
	for i := 0; i < len(accounts); i++ {
		addr, err := sdk.AccAddressFromBech32(accounts[i])
		if err != nil {
			return nil, err
		}
		evmAddr, found := h.k.GetEVMAddress(ctx, addr)
		if !found {
			return nil, types.NewAssociationMissingErr(addr.String())
		}
		evmAddrs = append(evmAddrs, evmAddr)
	}

	var tIds []*big.Int
	for i := 0; i < len(tokenIds); i++ {
		t, ok := sdk.NewIntFromString(tokenIds[i])
		if !ok {
			return nil, errors.New("invalid token ID for ERC1155, must be a big Int")
		}
		tIds = append(tIds, t.BigInt())
	}

	bz, err := abi.Pack("balanceOfBatch", evmAddrs, tIds)
	if err != nil {
		return nil, err
	}
	res, err := h.k.StaticCallEVM(ctx, callerAddr, &contract, bz)
	if err != nil {
		return nil, err
	}
	unpacked, err := abi.Unpack("balanceOfBatch", res)
	if err != nil {
		return nil, err
	}
	balances_res := unpacked[0].([]*big.Int)
	var balances []*sdk.Int
	for i := 0; i < len(balances_res); i++ {
		balance := sdk.NewIntFromBigInt(balances_res[i])
		balances = append(balances, &balance)
	}
	return json.Marshal(bindings.ERC1155BalanceOfBatchResponse{Balances: balances})
}

func (h *EVMQueryHandler) HandleERC1155Uri(ctx sdk.Context, caller string, contractAddress string, tokenId string) ([]byte, error) {
	callerAddr, err := sdk.AccAddressFromBech32(caller)
	if err != nil {
		return nil, err
	}
	t, ok := sdk.NewIntFromString(tokenId)
	if !ok {
		return nil, errors.New("invalid token ID for ERC1155, must be a big Int")
	}
	contract := common.HexToAddress(contractAddress)
	abi, err := cw1155.Cw1155MetaData.GetAbi()
	if err != nil {
		return nil, err
	}
	bz, err := abi.Pack("uri", t.BigInt())
	if err != nil {
		return nil, err
	}
	res, err := h.k.StaticCallEVM(ctx, callerAddr, &contract, bz)
	if err != nil {
		return nil, err
	}
	typed, err := abi.Unpack("uri", res)
	if err != nil {
		return nil, err
	}
	response := bindings.ERC1155UriResponse{Uri: typed[0].(string)}
	return json.Marshal(response)
}

func (h *EVMQueryHandler) HandleERC1155TotalSupply(ctx sdk.Context, caller string, contractAddress string) ([]byte, error) {
	callerAddr, err := sdk.AccAddressFromBech32(caller)
	if err != nil {
		return nil, err
	}
	contract := common.HexToAddress(contractAddress)
	abi, err := cw1155.Cw1155MetaData.GetAbi()
	if err != nil {
		return nil, err
	}
	bz, err := abi.Pack("totalSupply")
	if err != nil {
		return nil, err
	}
	res, err := h.k.StaticCallEVM(ctx, callerAddr, &contract, bz)
	if err != nil {
		return nil, err
	}
	typed, err := abi.Unpack("totalSupply", res)
	if err != nil {
		return nil, err
	}
	totalSupply := sdk.NewIntFromBigInt(typed[0].(*big.Int))
	response := bindings.ERC1155TotalSupplyResponse{Supply: &totalSupply}
	return json.Marshal(response)
}

func (h *EVMQueryHandler) HandleERC1155TotalSupplyForToken(ctx sdk.Context, caller string, contractAddress string, tokenId string) ([]byte, error) {
	callerAddr, err := sdk.AccAddressFromBech32(caller)
	if err != nil {
		return nil, err
	}
	t, ok := sdk.NewIntFromString(tokenId)
	if !ok {
		return nil, errors.New("invalid token ID for ERC1155, must be a big Int")
	}
	contract := common.HexToAddress(contractAddress)
	abi, err := cw1155.Cw1155MetaData.GetAbi()
	if err != nil {
		return nil, err
	}
	bz, err := abi.Pack("totalSupply0", t.BigInt())
	if err != nil {
		return nil, err
	}
	res, err := h.k.StaticCallEVM(ctx, callerAddr, &contract, bz)
	if err != nil {
		return nil, err
	}
	typed, err := abi.Unpack("totalSupply0", res)
	if err != nil {
		return nil, err
	}
	totalSupply := sdk.NewIntFromBigInt(typed[0].(*big.Int))
	response := bindings.ERC1155TotalSupplyResponse{Supply: &totalSupply}
	return json.Marshal(response)
}

func (h *EVMQueryHandler) HandleERC1155TokenExists(ctx sdk.Context, caller string, contractAddress string, tokenId string) ([]byte, error) {
	callerAddr, err := sdk.AccAddressFromBech32(caller)
	if err != nil {
		return nil, err
	}
	t, ok := sdk.NewIntFromString(tokenId)
	if !ok {
		return nil, errors.New("invalid token ID for ERC1155, must be a big Int")
	}
	contract := common.HexToAddress(contractAddress)
	abi, err := cw1155.Cw1155MetaData.GetAbi()
	if err != nil {
		return nil, err
	}
	bz, err := abi.Pack("exists", t.BigInt())
	if err != nil {
		return nil, err
	}
	res, err := h.k.StaticCallEVM(ctx, callerAddr, &contract, bz)
	if err != nil {
		return nil, err
	}
	typed, err := abi.Unpack("exists", res)
	if err != nil {
		return nil, err
	}
	response := bindings.ERC1155TokenExistsResponse{Exists: typed[0].(bool)}
	return json.Marshal(response)
}

func (h *EVMQueryHandler) HandleERC1155NameSymbol(ctx sdk.Context, caller string, contractAddress string) ([]byte, error) {
	callerAddr, err := sdk.AccAddressFromBech32(caller)
	if err != nil {
		return nil, err
	}
	contract := common.HexToAddress(contractAddress)
	abi, err := cw1155.Cw1155MetaData.GetAbi()
	if err != nil {
		return nil, err
	}
	bz, err := abi.Pack("name")
	if err != nil {
		return nil, err
	}
	res, err := h.k.StaticCallEVM(ctx, callerAddr, &contract, bz)
	if err != nil {
		return nil, err
	}
	typed, err := abi.Unpack("name", res)
	if err != nil {
		return nil, err
	}
	name := typed[0].(string)
	bz, err = abi.Pack("symbol")
	if err != nil {
		return nil, err
	}
	res, err = h.k.StaticCallEVM(ctx, callerAddr, &contract, bz)
	if err != nil {
		return nil, err
	}
	typed, err = abi.Unpack("symbol", res)
	if err != nil {
		return nil, err
	}
	symbol := typed[0].(string)
	response := bindings.ERC1155NameSymbolResponse{Name: name, Symbol: symbol}
	return json.Marshal(response)
}

func (h *EVMQueryHandler) HandleERC1155RoyaltyInfo(ctx sdk.Context, caller string, contractAddress string, tokenId string, salePrice *sdk.Int) ([]byte, error) {
	callerAddr, err := sdk.AccAddressFromBech32(caller)
	if err != nil {
		return nil, err
	}
	t, ok := sdk.NewIntFromString(tokenId)
	if !ok {
		return nil, errors.New("invalid token ID for ERC1155, must be a big Int")
	}
	contract := common.HexToAddress(contractAddress)
	abi, err := cw1155.Cw1155MetaData.GetAbi()
	if err != nil {
		return nil, err
	}
	bz, err := abi.Pack("royaltyInfo", t.BigInt(), salePrice.BigInt())
	if err != nil {
		return nil, err
	}
	res, err := h.k.StaticCallEVM(ctx, callerAddr, &contract, bz)
	if err != nil {
		return nil, err
	}
	typed, err := abi.Unpack("royaltyInfo", res)
	if err != nil {
		return nil, err
	}

	typedReceiver := typed[0].(common.Address)
	receiver := ""
	if (typedReceiver != common.Address{}) {
		receiver = h.k.GetSeiAddressOrDefault(ctx, typedReceiver).String()
	}
	royaltyAmount := sdk.NewIntFromBigInt(typed[1].(*big.Int))
	response := bindings.ERC1155RoyaltyInfoResponse{Receiver: receiver, RoyaltyAmount: &royaltyAmount}
	return json.Marshal(response)
}

func (h *EVMQueryHandler) HandleSupportsInterface(ctx sdk.Context, caller string, id string, contractAddress string) ([]byte, error) {
	callerAddr, err := sdk.AccAddressFromBech32(caller)
	if err != nil {
		return nil, err
	}
	contract := common.HexToAddress(contractAddress)
	abi, err := cw721.Cw721MetaData.GetAbi()
	if err != nil {
		return nil, err
	}
	aid := [4]byte{}
	idbz, err := hex.DecodeString(strings.TrimPrefix(id, "0x"))
	if err != nil {
		return nil, err
	}
	copy(aid[:], idbz)
	bz, err := abi.Pack("supportsInterface", aid)
	if err != nil {
		return nil, err
	}
	res, err := h.k.StaticCallEVM(ctx, callerAddr, &contract, bz)
	if err != nil {
		return nil, err
	}
	typed, err := abi.Unpack("supportsInterface", res)
	if err != nil {
		return nil, err
	}
	return json.Marshal(bindings.SupportsInterfaceResponse{Supported: typed[0].(bool)})
}

func (h *EVMQueryHandler) HandleGetEvmAddress(ctx sdk.Context, seiAddr string) ([]byte, error) {
	addr, err := sdk.AccAddressFromBech32(seiAddr)
	if err != nil {
		return nil, err
	}
	evmAddr, associated := h.k.GetEVMAddress(ctx, addr)
	response := bindings.GetEvmAddressResponse{EvmAddress: evmAddr.Hex(), Associated: associated}
	return json.Marshal(response)
}

```

**File:** wasmbinding/queries.go (L25-42)
```go
type QueryPlugin struct {
	oracleHandler       oraclewasm.OracleWasmQueryHandler
	epochHandler        epochwasm.EpochWasmQueryHandler
	tokenfactoryHandler tokenfactorywasm.TokenFactoryWasmQueryHandler
	evmHandler          evmwasm.EVMQueryHandler
	stakingKeeper       stakingkeeper.Keeper
}

// NewQueryPlugin returns a reference to a new QueryPlugin.
func NewQueryPlugin(oh *oraclewasm.OracleWasmQueryHandler, eh *epochwasm.EpochWasmQueryHandler, th *tokenfactorywasm.TokenFactoryWasmQueryHandler, evmh *evmwasm.EVMQueryHandler, sk stakingkeeper.Keeper) *QueryPlugin {
	return &QueryPlugin{
		oracleHandler:       *oh,
		epochHandler:        *eh,
		tokenfactoryHandler: *th,
		evmHandler:          *evmh,
		stakingKeeper:       sk,
	}
}
```
