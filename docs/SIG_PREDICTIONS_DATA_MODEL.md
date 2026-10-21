# SIG Predictions API: Market, Exchange, Market Node, and Contract

> **Source:** `sig-predictions-open-api.json` — *Super Market API*, OpenAPI 3.1.0, version 1.0.0.  
> **Scope:** Terminology, resource relationships, settlement semantics, and implications for the market-making framework.  
> **Examples:** Hypothetical unless explicitly noted as examples from the schema.

## 1. Core mental model

**A Market defines the question; its Market Nodes and Contracts define what must happen for the question to resolve; its Exchanges define what can be traded.**

| Entity | Purpose | Main identity / fields |
| --- | --- | --- |
| **Market** | Container for a prediction question and one or more tradable outcomes; owns lifecycle and context | `id`, `title`, `status`, `isComposite`, `isMultiOutcome`, `exchanges[]`, `contexts[]` |
| **Exchange** | Atomic tradable instrument (API terminology: *tradable contract*); owns a distinct order book and trade history in a trading context | `id` / `exchangeId`, `option`, `latestPrice`, `initialPrice` |
| **Market Node** | Entry in the prediction's *resolution tree* | `nodeType` = `operator` or `contract`; `node_id` in the read model |
| **Contract** | Leaf Market Node defining an event, condition, and settlement details | `contract_id`, `contract_type`, `title`, `settlement_date`, `settled_with`, `contract_details` |

The OpenAPI description sketches the model as:

~~~text
Market (container)
├── exchanges[]
│   ├── Binary: 1 exchange supporting YES / NO
│   ├── Multi-outcome: N exchanges, one per option
│   └── Each exchange: option, orderbook, trades
└── market_nodes[] (resolution structure)
    ├── operator node: AND / OR / NOT / IF
    │   └── children[]
    └── contract node (leaf)
        └── contract_details: type-specific data
~~~

**Two separate structures:** `exchanges[]` answers **"what can I trade and at what price?"**; the node tree answers **"what must happen for it to pay out?"**. Orders, trades, and positions target `exchangeId`, not `marketId` or `contract_id`.

The API's use of *contract* in two places is potentially confusing: an **Exchange** is the *tradable contract*, whereas a **Contract node** is a *resolution/condition object*. Treat them as separate data-model types even though they are related.

## 2. Market: the prediction container

Example prediction: **Will US unemployment be below 4% on a specified date?**

A Market holds the human-readable question, scheduling, state, classifications, tradable exchanges, and accessible pricing contexts.

| Field | Interpretation |
| --- | --- |
| `id` | Canonical market identifier |
| `title` | Question presented to traders |
| `status` | `open`, `closed`, or `settled` in the request's context |
| `settlementDate` | Scheduled resolution time, possibly null |
| `settledWith` / `settledOn` | Effective resolved outcome and time, possibly null |
| `isComposite` | Multiple conditions combined logically |
| `isMultiOutcome` | More than one Exchange |
| `exchanges[]` | Tradable outcome instruments |
| `contexts[]` | Accessible public/tournament-specific prices and settlement states |

`Market` is **not** the resource on which you place an order. To trade a market, first pick one of its `exchanges[]` and the intended pricing/trading context.

## 3. Exchange: the tradable instrument

Each Exchange belongs to a Market and has an outcome label `option`, such as `YES`, `Candidate A`, or `Over 3.5`. Its order book, prices, trades, orders, and positions are addressed by `exchangeId`.

### 3.1 Binary market: one exchange, two position sides

Hypothetical question: **Will BTC exceed $100,000 by year-end?**

~~~text
Market #26
└── Exchange #36 (option="YES")
    ├── YES position
    └── NO position
~~~

A binary market has **one Exchange**, *not* two separate YES and NO exchanges. Orders set:

- `side: "yes"` or `side: "no"`
- `action: "buy"` or `action: "sell"`

At a **hypothetical YES-normalized reference price** of 0.60, the complementary NO price is 0.40. For a binary unit-payout contract, `p_NO = 1 - p_YES`; this is a price convention, **not** an assertion that independent posted bid/ask quotes always complement exactly.

The API distinguishes price representations:
- Market/exchange `latestPrice` and realtime trade/fill prices are **YES-normalized**.
- `GET /portfolio/fills`'s `Fill.price` is priced in the **outcome that traded**; a NO fill at 0.30 there corresponds to 0.70 on the realtime YES-normalized feed.
- Normalize to a single convention in the venue adapter before computing fair value, fills, P&L, or inventory.

### 3.2 Multi-outcome market: multiple exchanges

Hypothetical question: **Who wins an election?**

| Exchange | `option` | Example YES price |
| --- | --- | ---: |
| #201 | Candidate A | 0.45 |
| #202 | Candidate B | 0.35 |
| #203 | Candidate C | 0.20 |

Each option has its **own** Exchange and YES/NO sides. Buying NO on Candidate A is **not** equivalent to buying YES on Candidate B if Candidate C can win.

**Do not assume every multi-outcome market is a mutually exclusive partition.** The specification says outcomes may be mutually exclusive **or independent**; imported range outcomes may settle independently. Therefore:
- Do not enforce `sum(prices) == 1` on every market.
- The combined-orderbook `overround` is the sum of implied probabilities where available.
- `hasArbitrageOpportunity` is only described as potentially true for **mutually exclusive** multi-outcome markets whose best prices sum below 1. It is a *potential*, not guaranteed, arbitrage.

## 4. Market Node: the resolution tree

A node is either:

| Node kind | Meaning |
| --- | --- |
| `operator` | Combines other nodes using `AND`, `OR`, `NOT`, or `IF`; contains `children[]` |
| `contract` | Leaf specifying a settlement condition and its metadata |

Example **composite** market: *BTC above $100k AND ETH above $5k by year-end*.

~~~text
Market (composite)
└── root: operator AND
    ├── contract: BTC above $100k
    └── contract: ETH above $5k
~~~

The compound event resolves true only when **both** conditions are true. An operator node itself is not an order-book instrument.

This illustrates the distinction:
- **Composite** describes *logical structure* of the question.
- **Multi-outcome** describes the number of *tradable Exchanges*.

Neither label by itself tells you the number of independently priced settlement events or guarantees statistical independence.

**Read endpoint:** `GET /markets/{id}/nodes` returns `market_id`, `root`, and `contexts`. Returned nodes are discriminated by `node_type` and use snake_case fields such as `node_id`, `contract_id`, `contract_type`, and `settled_with`. Market creation instead uses camelCase `rootNode` / `nodeType`.

## 5. Contract: the atomic event / settlement condition

A Contract is a *leaf node*. It records which event is being checked, the date, how it should be interpreted, and (when settled) what happened.

Supported `contractType` values in the `MarketNode` creation schema:

- `Freeform`
- `Election Outcome`
- `Sports Outcome`
- `Economic Indicator`
- `Financial Product`
- `Corporate Earnings`
- `Imported Market`
- `Existing Market`

The shape of `contractDetails` depends on `contractType`. Most contract creation nodes require a future `settlementDate`; the OpenAPI schema documents exceptions for `Imported Market` and certain monitored `Corporate Earnings` contracts.

The spec provides this illustrative Freeform contract-node submission:

~~~json
{
  "nodeType": "contract",
  "position": 0,
  "title": "Will it rain tomorrow in NYC?",
  "settlementDate": "2030-01-01T00:00:00.000Z",
  "contractType": "Freeform",
  "contractDetails": {
    "contractType": "Freeform",
    "description": "Resolves YES if measurable rain is recorded."
  },
  "settlementOptions": ["YES"]
}
~~~

A fetched `MarketTreeContractNode` has `contract_id`, which the schema says equals its own `node_id`. **That does not establish equality with an `exchangeId`.** The overview says a contract leaf maps to an Exchange, but do not infer a universal same-ID or one-to-one mapping from that description; use the identifiers the API actually returns.

An `Existing Market` leaf makes this relationship explicit by referring to another existing market's `marketId`, `marketTitle`, `exchangeId`, and exact `settlementOption`.

## 6. Trading context: a necessary fifth concept

The same canonical Market/Exchange may be available in **public** or multiple **tournament** contexts. The API provides `contexts[]` with public or tournament descriptors. Each tournament has its own virtual-currency orderbook, balance, traded prices, and potentially context-specific settlement state.

Important consequences:

1. **Same Exchange ID does not mean same book or price.** Price and book access must be scoped to the intended tournament.
2. An organization-bound key can discover several contexts for one canonical market. Market-wide `latestPrice` may be null if several tournament prices exist; read the particular `contexts[]` entry.
3. For an explicit tournament, pass `tournamentId` consistently to discovery, exchange reads, and orders. Omitting it on an organization-bound key usually resolves singular reads/orders to the organization's default tournament; an unbound key defaults to public-global.
4. **Use a composite instrument key**, e.g. `(venue, context_type, tournament_id | "public", exchange_id)`, for order-book caches, quote state, subscriptions, positions, and execution ledgers.

A tournament Exchange may have a resting bid and ask while `latestPrice` is null, because no trade has yet established a tournament-specific price.

## 7. API workflow: from market discovery to execution

~~~text
GET /markets
    └── Pick Market + context + Exchange ID
         ├── GET /markets/{id}/nodes            # resolution logic
         ├── GET /exchanges/{id}/orderbook       # exchange market data
         └── GET /exchanges/{id}/price           # price snapshot
                  ↓
             Price and risk check
                  ↓
             POST /orders                      # exchangeId + side + action
                  ↓
             Reconcile orders, fills and positions
~~~

Hypothetical order payload conforming to the `SingleOrderInput` shape (replace the IDs with those returned by discovery):

~~~json
{
  "idempotencyKey": "sig-example-order-001",
  "exchangeId": "36",
  "side": "yes",
  "action": "buy",
  "quantity": 100,
  "price": 0.42,
  "tournamentId": "550e8400-e29b-41d4-a716-446655440000"
}
~~~

Here:
- `exchangeId`: **what** is being traded.
- `side`: **which outcome exposure** (YES or NO).
- `action`: **buy or sell**.
- `quantity`: positive integer number of shares.
- `price`: limit price (fresh limits require ticks of **0.005** between 0.005 and 0.995); omit for market order.
- `tournamentId`: **where** the order executes and which isolated cash/book applies.
- `idempotencyKey`: required for single-order placement; use a new value per logical order and the same value for retries of that exact request.

For live trading, the source specifies best-effort one-second-batched realtime feeds. Establish and periodically reconcile against authoritative REST reads. Resynchronize on reconnect, token refresh, revision gap, or a batch carrying `resyncRequired`.

## 8. Implications for the market-maker architecture

Keep the SIG-specific resource structure **out of venue-neutral strategy logic**:

| Responsibility | Recommended location |
| --- | --- |
| Discover markets, choose a tournament, parse `exchanges[]` | SIG venue adapter / instrument catalog |
| Parse the Market Node resolution tree | Contract/settlement metadata model |
| Estimate event probability and cross-contract dependence | Strategy valuation layer |
| Normalize YES-vs-NO price, tick size, order-side conventions | SIG venue adapter |
| Maintain live book, handle versioning and reconnect recovery | Market-data adapter + canonical state |
| Submit/cancel orders by `exchangeId`; enforce idempotency and reconcile | Execution gateway / order manager |
| Partition books, balance, holdings and risk by tournament | Instrument identity, portfolio and risk scopes |

Suggested separation in Python terms (illustrative design, **not** API-provided class names):

~~~text
PredictionMarket
  ├── id, title, status
  ├── resolution_tree: MarketNode
  └── exchanges: list[TradableOutcome]

TradableOutcome
  ├── exchange_id, market_id, option
  └── context-specific Book / Quote / Position

MarketNode
  ├── OperatorNode(operator, children)
  └── ContractNode(contract_id, contract_type, contract_details)

TradingScope
  ├── venue = "sig"
  ├── context_type = "public" | "tournament"
  └── tournament_id: optional UUID
~~~

**Risk considerations:** multi-outcome and composite contracts may share underlying events; exposure must be aggregated at both exchange and event level. Do not mistake separately traded exchanges for statistically independent risks. Keep market-specific model/logic away from the execution adapter, while the adapter handles exact SIG order semantics.

## 9. Common pitfalls

| Incorrect assumption | Correct interpretation |
| --- | --- |
| A Market itself is the order instrument | An order must reference **`exchangeId`** |
| A binary market has one YES Exchange and one NO Exchange | A binary market has **one Exchange**, supporting YES/NO positions |
| Contract node = Exchange object | Contract node describes settlement; Exchange is tradable; IDs are not interchangeable |
| Composite = multi-outcome | Composite refers to Boolean logic; multi-outcome to exchange count |
| Multi-outcome prices always sum to 1 | Outcomes may be independent or range-based; exclusivity must be verified |
| Exchange price is universal across tournaments | Books, prices, settlement state, and balances can be context-specific |
| REST fills and realtime fill prices share one price-side convention | REST `Fill.price` is traded-side; realtime prices are YES-normalized |
| A realtime event feed alone is authoritative | REST is required for initial state, recovery, and periodic reconciliation |

## 10. Source pointers

Primary source: **`sig-predictions-open-api.json`** (Super Market API, OpenAPI 3.1.0).

Relevant schema names: `Market`, `Exchange`, `MarketPricingContext`, `MarketReadContextDescriptor`, `MarketSubmission`, `MarketNode`, `MarketContractDetails`, `MarketTreeNode`, `MarketTreeContractNode`, `CombinedMarketOrderbook`, `OrderInput`, `SingleOrderInput`, `Fill`.

Relevant endpoints: `GET /markets`, `GET /markets/{id}/nodes`, `GET /exchanges/{id}/price`, `GET /exchanges/{id}/orderbook`, `POST /orders`, `POST /realtime/token`.

Generated Python SDK/API reference: [kyle-nguyennn/super-market-api](https://github.com/kyle-nguyennn/super-market-api) (see its `api.md`).
