"""The Wealthsimple app's own FetchIdentityPositions document.

Extracted from the app's public JS bundle (compiled AST) and
re-printed to source, minus Relay client-only directives
(@nonreactive) that never reach the wire. The gateway rejects
modified or free-form queries but accepts this document with a
user token - and unlike the library's minimal variant it returns
marginRequirement, strategyType, and legs per position.

Keep byte-stable: the document is matched by content.
"""

FETCH_IDENTITY_POSITIONS = '''
query FetchIdentityPositions($identityId: ID!, $currency: Currency!, $first: Int, $cursor: String, $accountIds: [ID!], $aggregated: Boolean, $currencyOverride: CurrencyOverride, $sort: PositionSort, $sortDirection: PositionSortDirection, $filter: PositionFilter, $since: PointInTime, $includeSecurity: Boolean = false, $includeFundamentals: Boolean = false, $includeAccountData: Boolean = false, $includeOneDayReturnsBaseline: Boolean = false, $includeSleeveAllocations: Boolean = false, $accountScope: AccountScope) {
  identity(id: $identityId) {
    id
    financials(accountScope: $accountScope, filter: {accounts: $accountIds}) {
      current(currency: $currency) {
        id
        positions(first: $first, after: $cursor, aggregated: $aggregated, filter: $filter, sort: $sort, sortDirection: $sortDirection) {
          edges {
            node {
              ...PositionV2
            }
          }
          pageInfo {
            hasNextPage
            endCursor
          }
          totalCount
          status
          hasOptionsPosition
          hasCryptoPositionsOnly
          securityTypes
          securityCurrencies
        }
      }
    }
  }
}

fragment PositionV2 on PositionV2 {
  id
  quantity
  accounts @include(if: $includeAccountData) {
    id
  }
  percentageOfAccount
  positionDirection
  bookValue {
    amount
    currency
  }
  averagePrice {
    amount
    currency
  }
  marketAveragePrice: averagePrice(currencyOverride: $currencyOverride) {
    amount
    currency
  }
  marketBookValue: bookValue(currencyOverride: $currencyOverride) {
    amount
    currency
  }
  totalValue(currencyOverride: $currencyOverride) {
    amount
    currency
  }
  unrealizedReturns(since: $since) {
    amount
    currency
  }
  marketUnrealizedReturns: unrealizedReturns(currencyOverride: $currencyOverride) {
    amount
    currency
  }
  marginRequirement(currencyOverride: $currencyOverride) {
    amount
    currency
  }
  security {
    id
    ...SecuritySummaryWithoutMarketMetadata @include(if: $includeSecurity)
    fundamentals(currency: null) @include(if: $includeFundamentals) {
      currency
      high52Week
      low52Week
      yield
    }
  }
  oneDayReturnsBaselineV2(currencyOverride: $currencyOverride) @include(if: $includeOneDayReturnsBaseline) {
    baseline {
      currency
      amount
    }
    useDailyPriceChange
  }
  strategyType
  legs {
    ...PositionLeg
  }
  sleeveDetails {
    id
    referenceId
    allocations @include(if: $includeSleeveAllocations) {
      attributionPercentage
      security {
        id
      }
      quantity
      totalValue(currencyOverride: $currencyOverride) {
        amount
        currency
      }
      bookValue(currencyOverride: $currencyOverride) {
        amount
        currency
      }
      unrealizedReturns(since: $since) {
        amount
        currency
      }
    }
  }
}

fragment SecuritySummaryWithoutMarketMetadata on Security {
  ...SecuritySummaryDetails
  stock {
    ...StockSummary
  }
  quoteV2(currency: null) {
    ...SecurityQuoteV2
  }
  optionDetails {
    ...OptionSummary
  }
  futuresDetails {
    ...FuturesSummary
  }
}

fragment SecuritySummaryDetails on Security {
  id
  buyable
  currency
  inactiveDate
  status
  wsTradeEligible
  equityTradingSessionType
  securityType
  active
  securityGroups {
    id
    name
  }
  features
  logo
  logoUrl
  sellable
  optionsEligible
  depositEligible
  withdrawEligible
}

fragment StockSummary on Stock {
  ipoState
  name
  symbol
  primaryMic
  primaryExchange
}

fragment SecurityQuoteV2 on UnifiedQuote {
  ...StreamedSecurityQuoteV2
  previousBaseline
}

fragment StreamedSecurityQuoteV2 on UnifiedQuote {
  __typename
  securityId
  ask
  bid
  currency
  price
  sessionPrice
  quotedAsOf
  ... on EquityQuote {
    marketStatus
    askSize
    bidSize
    close
    high
    last
    lastSize
    low
    open
    mid
    volume: vol
    referenceClose
  }
  ... on OptionQuote {
    marketStatus
    askSize
    bidSize
    close
    high
    last
    lastSize
    low
    open
    mid
    volume: vol
    breakEven
    inTheMoney
    liquidityStatus
    openInterest
    underlyingSpot
  }
  ... on FutureQuote {
    marketStatus
    askSize
    bidSize
    close
    high
    last
    lastSize
    low
    open
    mid
    volume: vol
    openInterest
    settlementPrice
  }
  ... on PerpetualFutureQuote {
    perpOpenInterest: openInterest
    volume: vol
    settlementMark {
      assetPrice
    }
  }
}

fragment OptionSummary on Option {
  underlyingSecurity {
    ...UnderlyingSecuritySummary
  }
  maturity
  osiSymbol
  expiryDate
  multiplier
  optionType
  settlementTime
  strikePrice
}

fragment UnderlyingSecuritySummary on Security {
  id
  logo
  securityType
  stock {
    name
    primaryExchange
    primaryMic
    symbol
  }
}

fragment FuturesSummary on FuturesDetails {
  rootSymbol
  expiryDate
  lastTradeDate
  wsLastTradeDateTime
  firstTradeDate
  firstNoticeDate
  contractSize
  multiplier
  tickSize
  tickValue
  settlementMethod
  clientMarginLong
  clientMarginShort
  countryOfTrade
}

fragment PositionLeg on PositionLeg {
  security {
    id
    ...SecuritySummaryWithoutMarketMetadata @include(if: $includeSecurity)
    fundamentals(currency: null) @include(if: $includeFundamentals) {
      currency
      high52Week
      low52Week
      yield
    }
  }
  quantity
  positionDirection
  bookValue {
    amount
    currency
  }
  totalValue(currencyOverride: $currencyOverride) {
    amount
    currency
  }
  averagePrice {
    amount
    currency
  }
  percentageOfAccount
  unrealizedReturns(since: $since) {
    amount
    currency
  }
  marketAveragePrice: averagePrice(currencyOverride: $currencyOverride) {
    amount
    currency
  }
  marketBookValue: bookValue(currencyOverride: $currencyOverride) {
    amount
    currency
  }
  marketUnrealizedReturns: unrealizedReturns(currencyOverride: $currencyOverride) {
    amount
    currency
  }
  oneDayReturnsBaselineV2(currencyOverride: $currencyOverride) @include(if: $includeOneDayReturnsBaseline) {
    baseline {
      currency
      amount
    }
    useDailyPriceChange
  }
}
'''


def app_positions_variables(ws, account_ids):
    """Variables mirroring the app's own usage."""
    identity_id = getattr(ws, "identity_id", None)
    if not identity_id:
        try:
            ws._fetch_identity_id_from_token()
        except Exception:
            pass
        identity_id = getattr(ws, "identity_id", None)
    return {
        "identityId": identity_id,
        "currency": "CAD",
        "first": 500,
        "cursor": None,
        "accountIds": account_ids,
        "aggregated": False,
        "currencyOverride": "MARKET",
        "sort": None,
        "sortDirection": None,
        "filter": None,
        "since": None,
        "includeSecurity": True,
        "includeFundamentals": False,
        "includeAccountData": False,
        "includeOneDayReturnsBaseline": False,
        "includeSleeveAllocations": False,
        "accountScope": None,
    }
