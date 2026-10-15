import csv

from starlette.requests import Request

from joint_expense_tracker.db import get_db, init_db, personal_subcategories, save_subcategory
from joint_expense_tracker.models import ParsedAlert
from joint_expense_tracker.services import create_manual_transaction, insert_alert, update_transaction, classify_transaction, dashboard_data, export_month
from joint_expense_tracker import web


def test_subcategories_import_edit_classify_export(tmp_path, monkeypatch):
    path = tmp_path / 'test.sqlite'
    init_db(path)
    monkeypatch.setattr('joint_expense_tracker.services.get_db', lambda: get_db(path))
    with get_db(path) as conn:
        category = save_subcategory(conn, 'Personal', '  Video   games  ')
        assert category == 'Video games'
        assert save_subcategory(conn, 'Personal', 'video games') == category
        conn.execute("INSERT INTO rules (match_text, default_status, default_subcategory, priority) VALUES ('Arcade', 'Personal', ?, 1)", (category,))
        assert insert_alert(conn, ParsedAlert(25, 'Arcade', None, '2026-09-09', '2026-09', 'Arcade purchase'))
        row = conn.execute('SELECT * FROM transactions').fetchone()
        tx_id = row['id']
        assert (row['status'], row['subcategory'], row['joint_amount']) == ('Personal', category, 0)
        assert dashboard_data(conn, '2026-09')['total_personal'] == 25
        update_transaction(conn, tx_id, {'status': 'Personal', 'merchant': 'Arcade', 'subcategory': 'Hobbies'})
        assert 'Hobbies' in personal_subcategories(conn)
    classify_transaction(tx_id, 'Personal')
    with get_db(path) as conn:
        assert conn.execute('SELECT subcategory FROM transactions').fetchone()[0] == 'Hobbies'
    # export_month uses an explicit DB argument; restore the unpatched context manager.
    monkeypatch.setattr('joint_expense_tracker.services.get_db', get_db)
    csv_path, _ = export_month('2026-09', path, tmp_path)
    with csv_path.open() as f:
        assert next(csv.DictReader(f))['Subcategory'] == 'Hobbies'
    monkeypatch.setattr('joint_expense_tracker.services.get_db', lambda: get_db(path))
    classify_transaction(tx_id, 'Wife')
    with get_db(path) as conn:
        row = conn.execute('SELECT * FROM transactions').fetchone()
        assert row['subcategory'] is None
        assert row['status'] == 'Wife'
        assert dashboard_data(conn, '2026-09')['total_personal'] == 25


def test_manual_categories_filter_and_rule_updates(tmp_path, monkeypatch):
    path = tmp_path / 'test.sqlite'
    init_db(path)
    monkeypatch.setattr(web, 'get_db', lambda: get_db(path))
    with get_db(path) as conn:
        tx = create_manual_transaction(conn, '2026-09-09', 'Lunch', 10, status='Personal', subcategory='Food')
        create_manual_transaction(conn, '2026-09-09', 'Shared lunch', 20, status='Joint', subcategory='Food')
        assert conn.execute('SELECT subcategory FROM transactions WHERE status = ?', ('Joint',)).fetchone()[0] is None
        conn.execute("INSERT INTO rules (match_text, default_status) VALUES ('Lunch', 'Personal')")
        rule_id = conn.execute("SELECT id FROM rules WHERE match_text = 'Lunch'").fetchone()[0]
    web.set_rule_subcategory(rule_id, 'Food')
    web.save_rule(match_text='Bookstore', match_field='merchant', default_status='Personal',
                  default_subcategory='Books', default_joint_percent='', default_joint_amount='',
                  priority=5, active=1)
    with get_db(path) as conn:
        assert insert_alert(conn, ParsedAlert(8, 'Bookstore', None, '2026-09-09', '2026-09', 'Bookstore'))
        assert conn.execute("SELECT subcategory FROM transactions WHERE merchant = 'Bookstore'").fetchone()[0] == 'Books'
    request = Request({'type': 'http', 'method': 'GET', 'path': '/transactions', 'headers': [], 'app': web.app, 'scheme': 'http', 'server': ('testserver', 80), 'root_path': ''})
    response = web.transactions(request, subcategory='Food')
    assert [row['id'] for row in response.context['transactions']] == [tx]
    assert 'Food' in response.body.decode()
    assert 'Books' in web.rules(request).body.decode()
    assert 'personal-subcategories' not in web.dashboard(request, month='2026-09').body.decode()
    with get_db(path) as conn:
        assert conn.execute('SELECT default_subcategory FROM rules WHERE id = ?', (rule_id,)).fetchone()[0] == 'Food'
        update_transaction(conn, tx, {'status': 'Personal', 'subcategory': ''})
        assert conn.execute('SELECT subcategory FROM transactions WHERE id = ?', (tx,)).fetchone()[0] is None


def test_migration_preserves_existing_data(tmp_path):
    path = tmp_path / 'old.sqlite'
    init_db(path)
    with get_db(path) as conn:
        tx = create_manual_transaction(conn, '2026-09-09', 'Gift', 12, status='Wife')
        conn.execute('ALTER TABLE transactions DROP COLUMN subcategory')
        conn.execute('ALTER TABLE rules DROP COLUMN default_subcategory')
    init_db(path)
    init_db(path)
    with get_db(path) as conn:
        row = conn.execute('SELECT * FROM transactions WHERE id = ?', (tx,)).fetchone()
        assert (row['status'], row['amount'], row['subcategory']) == ('Wife', 12, None)
        assert personal_subcategories(conn) == ['Food', 'Video games']
