"""The Wealthsimple app's own FetchSecurity document.

Extracted from the app's public JS bundle like the positions
document; security.marginRates.clientMarginRate is the per-ticker
margin rate used for the account's margin requirement. Falls back
to the configured default rate when unavailable.

Keep byte-stable: the document is matched by content.
"""

FETCH_SECURITY = '''
query FetchSecurity($securityId: ID!, $currency: Currency, $earningsHistoryLimit: Int) {
  security(id: $securityId) {
    ...Security
  }
}

fragment Security on Security {
  active
  activeDate
  allowedOrderSubtypes
  buyable
  currency
  depositEligible
  features
  fundamentals(currency: $currency) {
    ...Fundamentals
  }
  optionDetails {
    greekSymbols {
      ...OptionGreekSymbols
    }
    underlyingSecurity {
      ...OptionUnderlyingSecurity
    }
    assetClass
    expiryDate
    maturity
    multiplier
    optionType
    osiSymbol
    settlementTime
    strikePrice
  }
  id
  inactiveDate
  isVolatile
  logo
  logoUrl
  marginRates {
    ...ClientMarginRates
  }
  managementExpenseRatio
  settlementPeriodBusinessDays
  mutualFundDetails {
    cutOffTime
    dividendFrequency
    fundCode
    loadType
    manufacturerId
    seriesType
  }
  marketMetadata {
    ...SecurityMarketMetadata
  }
  securityType
  securityGroups {
    ...PartialSecurityGroup
  }
  sellable
  settleable
  stakeable
  status
  stock {
    ...Stock
  }
  withdrawEligible
  wsTradeEligible
  wsTradeIneligibilityReason
  assetCategory
  optionsEligible
  equityTradingSessionType
  futuresDetails {
    ...FuturesDetails
  }
  perpetualFutureDetails {
    ...PerpetualFutureDetails
  }
  ipoDetails {
    ...IpoDetails
  }
  nextEarnings {
    ...EarningsEvent
  }
  earningsHistory(last: $earningsHistoryLimit) {
    ...EarningsEvent
  }
}

fragment Fundamentals on Fundamentals {
  avgVolume
  beta
  circulatingSupply
  companyCash
  companyCeo
  companyDebt
  companyEarningsGrowth
  companyGrossProfitMargin
  companyHqLocation
  companyRevenue
  currency
  dailyVolume
  description
  eps
  high52Week
  inceptionYear
  low52Week
  marketCap
  numberOfEmployees
  peRatio
  sharesOutstanding
  totalAssets
  totalSupply
  yield
}

fragment OptionGreekSymbols on OptionGreekSymbols {
  id
  rho
  vega
  delta
  theta
  gamma
  impliedVolatility
  calculationTime
}

fragment OptionUnderlyingSecurity on Security {
  id
  currency
  features
  logo
  securityType
  status
  stock {
    name
    symbol
    primaryMic
    primaryExchange
  }
}

fragment ClientMarginRates on MarginRates {
  clientMarginRate
}

fragment SecurityMarketMetadata on SecurityMarketMetadata {
  previousTradeDay {
    ...TradingDay
  }
  currentTradeDay {
    ...TradingDay
  }
  nextTradeDay {
    ...TradingDay
  }
}

fragment TradingDay on TradingDay {
  date
  overnight {
    ...TradingSessionBoundaries
  }
  preMarket {
    ...TradingSessionBoundaries
  }
  regular {
    ...TradingSessionBoundaries
  }
  postMarket {
    ...TradingSessionBoundaries
  }
}

fragment TradingSessionBoundaries on TradingSessionBoundaries {
  start
  end
}

fragment PartialSecurityGroup on PartialSecurityGroup {
  id
  name
}

fragment Stock on Stock {
  advancedChartSymbol
  description
  dividendFrequency
  ipoState
  leverageRatio
  name
  primaryExchange
  primaryMic
  segmentMic
  symbol
  usPtp
}

fragment FuturesDetails on FuturesDetails {
  advancedChartSymbol
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
  intradayMarginLong
  intradayMarginShort
  intradayMarginRate
  intradayMarginSession {
    ...TradingSessionBoundaries
  }
  countryOfTrade
}

fragment PerpetualFutureDetails on PerpetualFutureDetails {
  displaySymbol
  name
  ticker
  exchange
  rootSymbol
  preferredSymbol
  contractSize
  tickSize
  fractionalTradingEnabled
  firstTradeDate
  initialMarginRateLong
  initialMarginRateShort
  maintenanceMarginRateLong
  maintenanceMarginRateShort
  clientMarginRateLong
  clientMarginRateShort
  houseMarginRateLong
  houseMarginRateShort
  initialMarginMultiplier
  wealthsimpleBuffer
}

fragment IpoDetails on IpoDetails {
  allocationProcessedAt
  allocationsAllowed
  bidEligibility
  biddingCancellationClosesAt
  biddingClosesAt
  companyDescription
  companyDescriptionFr
  finalPricingDate
  initialFilingDate
  intendedSymbol
  ipoDate
  ipoPrice
  ipoPriceHigh
  ipoPriceLow
  leadUnderwriter
  lockUpPeriodDays
  offeringSize
  prospectusUrl
  prospectusUrlFr
  sharesOffered
}

fragment EarningsEvent on EarningsEvent {
  id
  announcementDate
  reportingTime
  periodEnding
  confirmed
  currency
  epsActual
  epsEstimated
  epsResult
  epsSurprisePercent
  revenueActual
  revenueEstimated
  revenueResult
  revenueSurprisePercent
  fiscalPeriod
  fiscalYear
}
'''


def security_variables(security_id):
    """Variables mirroring the app's own usage."""
    return {
        "securityId": security_id,
        "currency": None,
        "earningsHistoryLimit": None,
    }
