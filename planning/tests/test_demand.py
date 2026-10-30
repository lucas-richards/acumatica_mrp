import datetime as dt
import unittest
from decimal import Decimal as D

from planning import demand
from planning.demand import MonthlyQty

TODAY = dt.date(2026, 10, 29)


def m(sku, y, mo, qty):
    return MonthlyQty(sku, dt.date(y, mo, 1), D(qty))


class HelperTests(unittest.TestCase):
    def test_add_months_crosses_years(self):
        self.assertEqual(demand.add_months(dt.date(2026, 10, 1), 3), dt.date(2027, 1, 1))
        self.assertEqual(demand.add_months(dt.date(2026, 1, 1), -1), dt.date(2025, 12, 1))


class TrailingShipmentsTests(unittest.TestCase):
    def test_average_of_last_full_months_excludes_current_month(self):
        rows = [m('FG', 2026, mo, 10) for mo in range(4, 10)] + [m('FG', 2026, 10, 999), m('FG', 2026, 3, 999)]
        self.assertEqual(demand.trailing_shipments(rows, TODAY, 6), {'FG': D(10)})

    def test_missing_months_count_as_zero(self):
        rows = [m('FG', 2026, 9, 60)]
        self.assertEqual(demand.trailing_shipments(rows, TODAY, 6), {'FG': D(10)})

    def test_no_rows(self):
        self.assertEqual(demand.trailing_shipments([], TODAY, 6), {})

    def test_bucket_dates_inside_month_are_normalised(self):
        rows = [MonthlyQty('FG', dt.date(2026, 9, 17), D(30))]
        self.assertEqual(demand.trailing_shipments(rows, TODAY, 3), {'FG': D(10)})


class OpenSalesOrderTests(unittest.TestCase):
    def test_includes_past_due_and_horizon(self):
        rows = [m('FG', 2026, 8, 6), m('FG', 2026, 10, 12), m('FG', 2026, 12, 12), m('FG', 2027, 1, 999)]
        self.assertEqual(demand.open_sales_orders(rows, TODAY, 3), {'FG': D(10)})


class ForecastTests(unittest.TestCase):
    def test_next_months_from_current(self):
        rows = [m('FG', 2026, 9, 999), m('FG', 2026, 10, 30), m('FG', 2026, 11, 30), m('FG', 2026, 12, 30),
                m('FG', 2027, 1, 999)]
        self.assertEqual(demand.forecast(rows, TODAY, 3), {'FG': D(30)})


class RegistryTests(unittest.TestCase):
    def test_swappable_by_name(self):
        for name in ('trailing_shipments', 'open_sales_orders', 'forecast'):
            self.assertIs(demand.get_demand_function(name), demand.DEMAND_SOURCES[name])

    def test_unknown_source(self):
        with self.assertRaises(ValueError):
            demand.get_demand_function('magic')

    def test_window_must_be_positive(self):
        with self.assertRaises(ValueError):
            demand.trailing_shipments([], TODAY, 0)


if __name__ == '__main__':
    unittest.main()
