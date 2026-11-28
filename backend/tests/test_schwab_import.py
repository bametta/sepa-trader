"""Schwab positions CSV importer, against several plausible export layouts."""
import unittest
from datetime import date

from tests._loader import load

si = load("schwab_import")
TODAY = date(2026, 6, 1)

STANDARD = '''"Positions for account PCRA Roth ...1234 as of 04:20 PM ET, 11/26/2026"
""
"Symbol","Description","Quantity","Price","Price Change %","Price Change $","Market Value","Day Change %","Day Change $","Cost Basis","Gain/Loss $","Gain/Loss %","Ratings","Reinvest Dividends?","Capital Gains?","% Of Account","Security Type"
"VTI","VANGUARD TOTAL STOCK MARKET ETF","100","$250.00","0.5%","$1.25","$25,000.00","0.5%","$125.00","$20,000.00","$5,000.00","25%","--","No","N/A","60.2%","ETFs & Closed End Funds"
"BND","VANGUARD TOTAL BOND MARKET ETF","80.5","$71.38","0%","$0.00","$5,746.09","0%","$0.00","$5,900.00","($153.91)","-2.6%","--","No","N/A","13.8%","ETFs & Closed End Funds"
"SWVXX","SCHWAB VALUE ADVANTAGE MONEY FUND","1,000","$1.00","--","--","$1,000.00","--","--","--","--","--","--","--","--","2.4%","Money Market Fund"
"Cash & Cash Investments","--","--","--","--","--","$9,800.00","--","--","--","--","--","--","--","--","23.6%","Cash and Money Market"
"Account Total","--","--","--","--","--","$41,546.09","--","--","--","--","--","--","--","--","--","--"
'''


FID_HEADER = ("Account Number,Account Name,Symbol,Description,Quantity,Last Price,Last Price Change,Current Value,"
              "Today's Gain/Loss Dollar,Today's Gain/Loss Percent,Total Gain/Loss Dollar,Total Gain/Loss Percent,"
              "Percent Of Account,Cost Basis Total,Average Cost Basis,Type\n")
FID_FOOTER = '''
"The data and information in this spreadsheet is provided to you solely for your use and is not for distribution."
"Brokerage services are provided by Fidelity Brokerage Services LLC (FBS), 900 Salem Street, Smithfield, RI 02917."
"Date downloaded 11/27/2026 1:00 PM ET"
'''
# Type=Cash here is the ACCOUNT type (cash vs margin), and must not make every holding look like cash.
FIDELITY_ONE = (FID_HEADER +
    'Z12345678,Rollover IRA,SPAXX**,HELD IN MONEY MARKET,,,,$1234.56,,,,,5.2%,,,Cash\n'
    'Z12345678,Rollover IRA,VOO,VANGUARD S&P 500 ETF,20,$650.00,+$1.25,"$13,000.00",+$25.00,+0.2%,"+$3,000.00",+30%,55%,'
    '"$10,000.00",$500.00,Cash\n'
    'Z12345678,Rollover IRA,FXAIX,FIDELITY 500 INDEX FUND,15.5,$250.00,-$0.50,"$3,875.00",-$7.75,-0.2%,+$875.00,+29%,'
    '17%,"$3,000.00",$193.55,Cash\n'
    'Z12345678,Rollover IRA,Pending Activity,,,,,-$200.00,,,,,,,,\n' + FID_FOOTER)
FIDELITY_TWO = (FID_HEADER +
    'Z11111111,Rollover IRA,VOO,VANGUARD S&P 500 ETF,20,$650.00,+$1.25,"$13,000.00",,,,,,,,Cash\n'
    'Z22222222,Joint Brokerage,AAPL,APPLE INC,10,$300.00,+$2.00,"$3,000.00",,,,,,,,Margin\n'
    'Z22222222,Joint Brokerage,SPAXX**,HELD IN MONEY MARKET,,,,$500.00,,,,,,,,Margin\n' + FID_FOOTER)


SCHWAB_REAL_STYLE = '''"Positions for account PCRA Roth ...0001 as of 11/27/2026"
""
"Symbol","Description","Qty (Quantity)","Price","Price Chng % (Price Change %)","Price Chng $ (Price Change $)","Mkt Val (Market Value)","Day Chng % (Day Change %)","Day Chng $ (Day Change $)","Cost Basis","Gain $ (Gain/Loss $)","Gain % (Gain/Loss %)","Ratings","Reinvest?","Reinvest Capital Gains?","% of Acct (% of Account)","Asset Type","Security Type"
"QQQM","INVESCO NASDAQ 100 ETF","25.4","$267.80","0.5%","$1.30","$6,801.89","0.5%","$33.02","$6,000.00","$801.89","13.4%","--","No","N/A","41.8%","Equity","ETFs & Closed End Funds"
"SMH","VANECK SEMICONDUCTOR ETF","7.25","$440.00","-0.3%","-$1.32","$3,190.00","-0.3%","-$9.57","$3,000.00","$190.00","6.3%","--","No","N/A","19.6%","Equity","ETFs & Closed End Funds"
"Cash & Cash Investments","--","--","--","--","--","$1,554.26","--","--","--","--","--","--","--","--","9.6%","Cash and Money Market","Cash and Money Market"
"Account Total","--","--","--","--","--","$11,546.15","--","--","--","--","--","--","--","--","--","--","--"
'''


class SchwabHeaderTests(unittest.TestCase):
    """Schwab's real export names columns like 'Qty (Quantity)' and 'Mkt Val (Market Value)'."""

    def test_quantity_column_with_a_bracketed_name_is_read(self):
        by = {h["symbol"] or h["name"]: h for h in si.parse_positions(SCHWAB_REAL_STYLE, TODAY)["holdings"]}
        self.assertEqual(by["QQQM"]["quantity"], 25.4)
        self.assertEqual(by["SMH"]["quantity"], 7.25)
        self.assertEqual(by["QQQM"]["manual_value"], 6801.89)
        self.assertEqual(by["Cash & Cash Investments"]["asset_class"], "cash")

    def test_totals_still_reconcile(self):
        out = si.parse_positions(SCHWAB_REAL_STYLE, TODAY)
        self.assertEqual(out["file_total"], 11546.15)
        self.assertEqual(out["warnings"], [])

    def test_quantity_variants(self):
        for header in ("Quantity", "Qty", "Qty (Quantity)", "Shares"):
            with self.subTest(header=header):
                text = f"Symbol,{header},Market Value\nVTI,3,$900\n"
                self.assertEqual(si.parse_positions(text, TODAY)["holdings"][0]["quantity"], 3.0)


class FidelityTests(unittest.TestCase):
    def parse(self, text, key=None):
        return si.parse_positions(text, TODAY, key)

    def test_single_account_file_is_recognised(self):
        out = self.parse(FIDELITY_ONE)
        by = {h["symbol"]: h for h in out["holdings"]}
        self.assertEqual(set(by), {"SPAXX", "VOO", "FXAIX"})
        self.assertEqual((by["VOO"]["quantity"], by["VOO"]["manual_value"]), (20.0, 13000.0))
        self.assertEqual(by["FXAIX"]["quantity"], 15.5)
        self.assertFalse(out["needs_choice"])

    def test_account_type_column_does_not_turn_holdings_into_cash(self):
        by = {h["symbol"]: h for h in self.parse(FIDELITY_ONE)["holdings"]}
        self.assertEqual(by["VOO"]["asset_class"], "auto")
        self.assertEqual(by["FXAIX"]["asset_class"], "auto")

    def test_core_cash_with_asterisks_is_cash_and_keeps_its_value(self):
        spaxx = [h for h in self.parse(FIDELITY_ONE)["holdings"] if h["symbol"] == "SPAXX"][0]
        self.assertEqual((spaxx["asset_class"], spaxx["manual_value"]), ("cash", 1234.56))
        self.assertIsNone(spaxx["quantity"])

    def test_pending_activity_is_skipped_with_a_note_and_footers_are_silent(self):
        out = self.parse(FIDELITY_ONE)
        self.assertEqual(len(out["warnings"]), 1)
        self.assertIn("Pending Activity", out["warnings"][0])

    def test_signed_dollar_formats(self):
        for raw, want in (("+$12.34", 12.34), ("-$1.00", -1.0), ("$1,000.50", 1000.5)):
            self.assertEqual(si.money(raw), want)

    def test_several_accounts_require_a_choice_and_labels_are_masked(self):
        out = self.parse(FIDELITY_TWO)
        self.assertTrue(out["needs_choice"])
        self.assertEqual(out["holdings"], [])
        labels = {g["key"]: g for g in out["groups"]}
        self.assertEqual(set(labels), {"Z11111111", "Z22222222"})
        self.assertIn("...1111", labels["Z11111111"]["label"])
        self.assertNotIn("Z11111111", labels["Z11111111"]["label"])        # only the last four characters show
        self.assertEqual((labels["Z22222222"]["holdings"], labels["Z22222222"]["total"]), (2, 3500.0))

    def test_choosing_an_account_imports_only_that_account(self):
        out = self.parse(FIDELITY_TWO, "Z22222222")
        self.assertEqual(sorted(h["symbol"] for h in out["holdings"]), ["AAPL", "SPAXX"])
        self.assertEqual(self.parse(FIDELITY_TWO, "Z11111111")["holdings"][0]["symbol"], "VOO")
        with self.assertRaises(ValueError):
            self.parse(FIDELITY_TWO, "Z99999999")

    def test_schwab_files_are_unaffected_by_the_group_logic(self):
        out = si.parse_positions(STANDARD, TODAY)
        self.assertFalse(out["needs_choice"])
        self.assertEqual(len(out["holdings"]), 4)

    def test_unknown_header_still_raises_a_helpful_error(self):
        with self.assertRaises(ValueError) as cm:
            self.parse("Name,Amount\nA,1\n")
        self.assertIn("first few lines", str(cm.exception))


class ParseTests(unittest.TestCase):
    def parse(self, text):
        return si.parse_positions(text, TODAY)

    def test_standard_export(self):
        out = self.parse(STANDARD)
        by = {h["symbol"] or h["name"]: h for h in out["holdings"]}
        self.assertEqual(set(by), {"VTI", "BND", "SWVXX", "Cash & Cash Investments"})
        self.assertEqual((by["VTI"]["quantity"], by["VTI"]["manual_value"]), (100.0, 25000.0))
        self.assertEqual(by["BND"]["quantity"], 80.5)
        self.assertEqual(by["SWVXX"]["quantity"], 1000.0)                     # thousands separator
        self.assertEqual(by["SWVXX"]["asset_class"], "cash")                  # money market fund
        self.assertEqual(by["Cash & Cash Investments"]["asset_class"], "cash")
        self.assertIsNone(by["Cash & Cash Investments"]["symbol"])
        self.assertEqual(out["file_total"], 41546.09)
        self.assertEqual(out["parsed_total"], 41546.09)
        self.assertEqual(out["warnings"], [])
        self.assertIn("PCRA Roth", out["title"])

    def test_header_without_title_and_in_other_order(self):
        text = 'Market Value,Quantity,Description,Symbol\n"$500.00","5","Some ETF","XYZ"\n'
        h = self.parse(text)["holdings"][0]
        self.assertEqual((h["symbol"], h["quantity"], h["manual_value"]), ("XYZ", 5.0, 500.0))

    def test_bom_blank_lines_and_extra_columns(self):
        text = "﻿\n\nSymbol,Quantity,Market Value,Mystery\n\nVTI,10,$2500,zzz\n\n"
        self.assertEqual(len(self.parse(text)["holdings"]), 1)

    def test_parentheses_negative_and_dashes(self):
        self.assertEqual(si.money("($153.91)"), -153.91)
        self.assertIsNone(si.money("--"))
        self.assertIsNone(si.money("N/A"))
        self.assertEqual(si.money("$1,234.50"), 1234.5)

    def test_position_with_only_a_value_becomes_a_manual_holding(self):
        text = "Symbol,Description,Quantity,Market Value\nSTABLE,STABLE VALUE FUND,--,$10000\n"
        h = self.parse(text)["holdings"][0]
        self.assertIsNone(h["quantity"])
        self.assertEqual(h["manual_value"], 10000.0)

    def test_unusable_rows_are_skipped_with_a_warning(self):
        text = ("Symbol,Quantity,Market Value\nVTI,10,$2500\n"
                "??BAD??,5,$100\nSHORT,-5,$100\nEMPTY,--,--\n")
        out = self.parse(text)
        self.assertEqual([h["symbol"] for h in out["holdings"]], ["VTI"])
        self.assertEqual(len(out["warnings"]), 3)

    def test_total_mismatch_is_reported(self):
        text = "Symbol,Quantity,Market Value\nVTI,10,$2500\nAccount Total,--,$9000\n"
        self.assertTrue(any("account total" in w for w in self.parse(text)["warnings"]))

    def test_lowercase_symbols_are_normalised(self):
        self.assertEqual(self.parse("Symbol,Quantity\nvti,3\n")["holdings"][0]["symbol"], "VTI")

    def test_unreadable_inputs_raise_helpful_errors(self):
        for bad in ("", "   ", "a,b,c\n1,2,3\n", "Symbol,Description\nVTI,x\n", "Symbol,Quantity\n"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.parse(bad)
        with self.assertRaises(ValueError) as cm:
            self.parse("a,b,c\n1,2,3\n")
        self.assertIn("first few lines", str(cm.exception))

    def test_detection(self):
        self.assertTrue(si.looks_like_broker_export(STANDARD))
        self.assertTrue(si.looks_like_broker_export(FIDELITY_ONE))
        self.assertFalse(si.looks_like_broker_export("symbol,name,quantity,value,asset_class\nVTI,,10,,auto\n"))
        self.assertFalse(si.looks_like_broker_export("hello,world\n1,2\n"))

    def test_output_matches_the_holdings_shape_the_tracker_stores(self):
        for h in self.parse(STANDARD)["holdings"]:
            self.assertEqual(set(h), {"symbol", "name", "quantity", "manual_value", "asset_class", "illiquid", "as_of"})
            self.assertEqual(h["as_of"], TODAY)


# Equity Trust "Portfolio Positions" layout: real header and Asset Type names; the rows are invented sample data.
ET_HEADER = "Asset Type\tAsset Number\tDescription\tAsset Value\tTicker Symbol\tUnits\tPrice\tLocation"
ET_TAB = "\n".join([
    "Portfolio Positions 11-27-2026", ET_HEADER,
    "Digital Currency\t9001\tBitcoin\t$12,500.00\t\t0.15\t\t",
    "Digital Currency\t9002\tEthereum\t$3,100.50\t\t\t\t",
    "Brokerage Accounts\t9003\tOther broker account\t$8,000.00\t\t\t\t",
    "Limited Liability Company\t9004\tSmith Holdings LLC\t$45,000.00\t\t\t\tTexas",
    "Real Estate\t9005\tRental duplex\t$310,000.00\t\t\t\t",
    "Mutual Fund\t9006\tVanguard Total Stock\t$5,000.00\tVTI\t20\t250.00\t",
    "Total\t\t\t$383,600.50\t\t\t\t",
])
ET_CSV = "\n".join([
    "Portfolio Positions", ET_HEADER.replace("\t", ","),
    'Digital Currency,9001,Bitcoin,"$12,500.00",,0.15,,',
    'Limited Liability Company,9004,Smith Holdings LLC,"$45,000.00",,,,Texas',
])
ET_NOW = date(2026, 11, 28)


class EquityTrustTests(unittest.TestCase):
    def parse(self, raw, today=ET_NOW):
        return si.parse_positions(raw, today=today)

    def test_header_is_recognised_in_tab_and_comma_form(self):
        self.assertTrue(si.looks_like_broker_export(ET_TAB))
        self.assertTrue(si.looks_like_broker_export(ET_CSV))

    def test_asset_types_map_to_classes_and_liquidity(self):
        by = {h["name"]: h for h in self.parse(ET_TAB)["holdings"]}
        self.assertEqual((by["Bitcoin"]["asset_class"], by["Bitcoin"]["illiquid"]), ("crypto", False))
        self.assertEqual(by["Ethereum"]["asset_class"], "crypto")
        self.assertEqual((by["Other broker account"]["asset_class"], by["Other broker account"]["illiquid"]), ("auto", False))
        self.assertEqual((by["Smith Holdings LLC"]["asset_class"], by["Smith Holdings LLC"]["illiquid"]), ("alt_private", True))
        self.assertEqual((by["Rental duplex"]["asset_class"], by["Rental duplex"]["illiquid"]), ("alt_real_estate", True))

    def test_values_and_total_match_with_no_mismatch_warning(self):
        p = self.parse(ET_TAB)
        self.assertEqual(len(p["holdings"]), 6)
        self.assertAlmostEqual(p["parsed_total"], 383600.50)
        self.assertEqual(p["file_total"], 383600.50)
        self.assertFalse(any("add up" in w for w in p["warnings"]))

    def test_digital_currency_with_custodian_codes_becomes_priced_crypto(self):
        raw = "\n".join([
            "Portfolio Positions 11-27-2026", ET_HEADER,
            "Digital Currency\t9001\tBitcoin\t$1,234.56\tDCXBT\t0.02\t\t",
            "Digital Currency\t9002\tSolana\t$987.65\tDCSOL\t5.5\t\t",
            "Digital Currency\t9003\tEthereum\t$1,000.00\tETH\t0.3\t\t",
        ])
        by = {h["name"]: h for h in self.parse(raw)["holdings"]}
        self.assertEqual((by["Bitcoin"]["symbol"], by["Bitcoin"]["asset_class"]), ("BTC-USD", "crypto"))
        self.assertEqual(by["Solana"]["symbol"], "SOL-USD")
        self.assertEqual(by["Ethereum"]["symbol"], "ETH-USD")                 # no DC prefix: used as is
        self.assertEqual(by["Bitcoin"]["quantity"], 0.02)
        self.assertEqual(by["Bitcoin"]["manual_value"], 1234.56)               # statement value kept as fallback
        self.assertFalse(by["Bitcoin"]["illiquid"])

    def test_non_crypto_ticker_rows_are_not_touched_by_the_crypto_rule(self):
        vti = next(h for h in self.parse(ET_TAB)["holdings"] if h["symbol"] == "VTI")
        self.assertEqual(vti["asset_class"], "auto")

    def test_a_ticker_row_keeps_its_symbol_and_units(self):
        vti = next(h for h in self.parse(ET_TAB)["holdings"] if h["symbol"] == "VTI")
        self.assertEqual((vti["quantity"], vti["manual_value"]), (20.0, 5000.0))

    def test_rows_are_dated_by_the_file_and_future_dates_fall_back_to_today(self):
        self.assertTrue(all(h["as_of"] == date(2026, 11, 27) for h in self.parse(ET_TAB)["holdings"]))
        early = self.parse(ET_TAB, today=date(2026, 6, 1))                 # file date is in the future -> today
        self.assertTrue(all(h["as_of"] == date(2026, 6, 1) for h in early["holdings"]))

    def test_title_without_a_date_uses_today(self):
        self.assertTrue(all(h["as_of"] == ET_NOW for h in self.parse(ET_CSV)["holdings"]))

    def test_tickerless_rows_are_not_imported_from_other_brokers(self):
        raw = STANDARD.replace('"Account Total"', '"","UNKNOWN THING","1","$1.00","--","--","$777.00","--","--","--","--","--","--","--","--","--","Other"\n"Account Total"')
        self.assertFalse(any(h["manual_value"] == 777.0 for h in si.parse_positions(raw, today=TODAY)["holdings"]))

    def test_zero_or_missing_value_rows_are_skipped_with_a_warning(self):
        raw = ET_TAB.replace("$310,000.00", "")
        p = self.parse(raw)
        self.assertEqual(len(p["holdings"]), 5)
        self.assertTrue(any("Rental duplex" in w for w in p["warnings"]))


if __name__ == "__main__":
    unittest.main()
