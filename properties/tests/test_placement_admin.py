"""Тариф «Розміщення квартири», структура платежів, цілісність БД, адмінпанель, вихід."""

from datetime import datetime, timedelta
from decimal import Decimal
from io import StringIO

from django.contrib.auth.models import User
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.db.models import ProtectedError
from django.test import TestCase, override_settings
from django.utils import timezone

from properties.models import (
    ModerationStatus,
    Payment,
    PaymentStatus,
    PlacementTransition,
    Property,
    Tariff,
)
from properties.placement import (
    PlacementError,
    apply_successful_payment,
    apply_transition,
    create_payment,
    mark_unsuccessful,
)

from .helpers import make_admin, make_property, make_user


def _pay(payment, amount=None, currency="UAH", txn=None):
    return apply_successful_payment(
        payment.pk, paid_amount=amount if amount is not None else payment.amount,
        paid_currency=currency, transaction_id=txn,
    )


class TariffTests(TestCase):
    def test_placement_tariff_seeded(self):
        t = Tariff.placement()
        self.assertEqual((t.name, t.price, t.currency, t.duration_days, t.is_active),
                         ("Розміщення квартири", Decimal("99.00"), "UAH", 30, True))
        # Один тариф розміщення; решта — тарифи просування (міграція 0029).
        self.assertEqual(Tariff.objects.filter(promotion_rank__isnull=True).count(), 1)

    def test_tariff_edit_does_not_change_existing_payment(self):
        owner = make_user("o")
        payment = create_payment(owner, make_property(owner=owner))
        Tariff.objects.filter(code="placement").update(price=Decimal("150.00"), duration_days=10, name="Нова")
        payment.refresh_from_db()
        self.assertEqual((payment.amount, payment.duration_days, payment.tariff_name),
                         (Decimal("99.00"), 30, "Розміщення квартири"))

    def test_tariff_with_payments_cannot_be_deleted(self):
        owner = make_user("o")
        create_payment(owner, make_property(owner=owner))
        with self.assertRaises(ProtectedError):
            Tariff.placement().delete()


class PlacementPaymentTests(TestCase):
    def setUp(self):
        self.owner = make_user("owner")
        self.prop = make_property(owner=self.owner)

    def test_create_payment_snapshot_and_owner_only(self):
        p = create_payment(self.owner, self.prop)
        self.assertEqual(p.status, PaymentStatus.CREATED)
        self.assertEqual(p.user_snapshot, "owner <owner@example.com>")
        self.assertTrue(p.order_id.startswith(f"mh-{self.prop.pk}-"))
        with self.assertRaises(PlacementError):
            create_payment(make_user("stranger"), self.prop)
        self.prop.archived_at = timezone.now()
        self.prop.save()
        with self.assertRaises(PlacementError):
            create_payment(self.owner, self.prop)

    def test_inactive_tariff_cannot_be_used(self):
        Tariff.objects.update(is_active=False)
        with self.assertRaises(PlacementError):
            create_payment(self.owner, self.prop)

    def test_first_payment_gives_30_days_and_records_details(self):
        before = timezone.now()
        p, applied = _pay(create_payment(self.owner, self.prop), txn="T-1")
        self.assertTrue(applied)
        self.prop.refresh_from_db()
        self.assertEqual(p.status, PaymentStatus.SUCCESS)
        self.assertEqual((p.paid_amount, p.paid_currency, p.provider_transaction_id),
                         (Decimal("99.00"), "UAH", "T-1"))
        self.assertIsNotNone(p.paid_at)
        self.assertGreaterEqual(p.period_start, before)
        self.assertEqual(p.period_end - p.period_start, timedelta(days=30))
        self.assertEqual(self.prop.placement_until, p.period_end)

    def test_idempotent_double_processing(self):
        payment = create_payment(self.owner, self.prop)
        _pay(payment, txn="T-1")
        self.prop.refresh_from_db()
        first_end = self.prop.placement_until
        p, applied = _pay(payment, txn="T-1")
        self.assertFalse(applied)
        self.prop.refresh_from_db()
        self.assertEqual(self.prop.placement_until, first_end)

    def test_renewal_of_active_listing_adds_to_current_end(self):
        current_end = timezone.now() + timedelta(days=10)
        Property.objects.filter(pk=self.prop.pk).update(placement_until=current_end)
        p, _ = _pay(create_payment(self.owner, self.prop))
        self.assertEqual(p.period_start, current_end)
        self.assertEqual(p.period_end, current_end + timedelta(days=30))

    def test_payment_for_new_listing_starts_on_publication(self):
        pending = make_property(owner=self.owner, public=False)
        payment, credited = _pay(create_payment(self.owner, pending))
        self.assertTrue(credited)
        pending.refresh_from_db()
        # Оплата не публікує і не запускає відлік
        self.assertEqual(pending.moderation_status, ModerationStatus.PENDING)
        self.assertFalse(pending.is_published)
        self.assertIsNone(pending.placement_until)
        self.assertIsNone(payment.activated_at)
        self.assertFalse(Property.objects.public().filter(pk=pending.pk).exists())
        # Схвалення адміністратором -> старт 30 днів
        self.client.force_login(make_admin())
        before = timezone.now()
        self.client.post(f"/admin-action/property/{pending.pk}/approve/")
        pending.refresh_from_db()
        payment.refresh_from_db()
        self.assertIsNotNone(payment.activated_at)
        self.assertGreaterEqual(payment.period_start, before)
        self.assertEqual(payment.period_end - payment.period_start, timedelta(days=30))
        self.assertEqual(pending.placement_until, payment.period_end)

    def test_expired_listing_returns_to_moderation_and_starts_on_republication(self):
        Property.objects.filter(pk=self.prop.pk).update(placement_until=timezone.now() - timedelta(days=5))
        payment, credited = _pay(create_payment(self.owner, self.prop))
        self.assertTrue(credited)
        self.prop.refresh_from_db()
        self.assertEqual(self.prop.moderation_status, ModerationStatus.PENDING)
        self.assertLess(self.prop.placement_until, timezone.now())  # оплата не продовжила
        self.assertIsNone(payment.period_start)
        # Повторна публікація через масову дію адмінки
        self.client.force_login(make_admin())
        before = timezone.now()
        self.client.post("/admin/properties/property/",
                         {"action": "make_approved", "_selected_action": [self.prop.pk]})
        payment.refresh_from_db()
        self.prop.refresh_from_db()
        self.assertGreaterEqual(payment.period_start, before)
        self.assertEqual(self.prop.placement_until, payment.period_end)
        self.assertEqual(payment.period_end - payment.period_start, timedelta(days=30))

    def test_two_payments_before_publication_chain(self):
        pending = make_property(owner=self.owner, public=False)
        p1, _ = _pay(create_payment(self.owner, pending))
        p2, _ = _pay(create_payment(self.owner, pending))
        pending.is_published = True
        pending.moderation_status = ModerationStatus.APPROVED
        pending.is_verified_by_admin = True
        pending.save()  # схвалення через збереження моделі (форма адмінки / list_editable)
        p1.refresh_from_db()
        p2.refresh_from_db()
        pending.refresh_from_db()
        self.assertEqual(p2.period_start, p1.period_end)
        self.assertEqual(pending.placement_until, p1.period_start + timedelta(days=60))

    def test_rejected_listing_does_not_activate(self):
        pending = make_property(owner=self.owner, public=False)
        payment, _ = _pay(create_payment(self.owner, pending))
        self.client.force_login(make_admin())
        self.client.post(f"/admin-action/property/{pending.pk}/reject/", {"moderation_note": "x"})
        payment.refresh_from_db()
        self.assertIsNone(payment.activated_at)
        self.assertEqual(payment.status, PaymentStatus.SUCCESS)

    def test_approved_but_unpaid_listing_starts_at_payment(self):
        p, _ = _pay(create_payment(self.owner, self.prop))
        self.assertIsNotNone(p.activated_at)
        self.assertEqual(p.period_end - p.period_start, timedelta(days=30))

    def test_amount_or_currency_mismatch_not_applied(self):
        for amount, currency in ((Decimal("1.00"), "UAH"), (Decimal("99.00"), "USD")):
            payment = create_payment(self.owner, self.prop)
            p, applied = _pay(payment, amount=amount, currency=currency)
            self.assertFalse(applied)
            self.assertEqual(p.status, PaymentStatus.PENDING)
            self.assertIn("Невідповідність", p.error_message)
        self.prop.refresh_from_db()
        self.assertIsNone(self.prop.placement_until)

    def test_same_transaction_id_cannot_pay_twice(self):
        _pay(create_payment(self.owner, self.prop), txn="DUP")
        second = create_payment(self.owner, self.prop)
        self.prop.refresh_from_db()
        end_before = self.prop.placement_until
        with self.assertRaises(PlacementError):
            _pay(second, txn="DUP")
        self.prop.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(self.prop.placement_until, end_before)
        self.assertNotEqual(second.status, PaymentStatus.SUCCESS)

    def test_failed_cancelled_expired_do_not_extend(self):
        for status in (PaymentStatus.FAILURE, PaymentStatus.CANCELLED, PaymentStatus.EXPIRED):
            p = mark_unsuccessful(create_payment(self.owner, self.prop).pk, status, "відмова банку")
            self.assertEqual(p.status, status)
        self.prop.refresh_from_db()
        self.assertIsNone(self.prop.placement_until)

    def test_late_failure_does_not_override_success(self):
        payment = create_payment(self.owner, self.prop)
        _pay(payment)
        p = mark_unsuccessful(payment.pk, PaymentStatus.FAILURE)
        self.assertEqual(p.status, PaymentStatus.SUCCESS)

    @override_settings(PLACEMENT_PAYMENT_REQUIRED=True)
    def test_catalog_requires_active_placement_when_enabled(self):
        self.assertFalse(Property.objects.public().filter(pk=self.prop.pk).exists())
        _pay(create_payment(self.owner, self.prop))
        self.assertTrue(Property.objects.public().filter(pk=self.prop.pk).exists())
        Property.objects.filter(pk=self.prop.pk).update(placement_until=timezone.now() - timedelta(seconds=1))
        self.assertFalse(Property.objects.public().filter(pk=self.prop.pk).exists())

    def test_catalog_unchanged_while_flag_off(self):
        self.assertTrue(Property.objects.public().filter(pk=self.prop.pk).exists())


class FinancialIntegrityTests(TestCase):
    def setUp(self):
        self.owner = make_user("owner")
        self.prop = make_property(owner=self.owner)

    def test_deleting_listing_with_payment_archives_it(self):
        payment = create_payment(self.owner, self.prop)
        self.client.force_login(self.owner)
        self.client.post(f"/property/{self.prop.pk}/delete/")
        self.prop.refresh_from_db()
        self.assertIsNotNone(self.prop.archived_at)
        self.assertTrue(Payment.objects.filter(pk=payment.pk).exists())
        self.assertFalse(Property.objects.public().filter(pk=self.prop.pk).exists())
        self.assertEqual(self.client.get(f"/property/{self.prop.pk}/").status_code, 404)
        self.assertNotIn(self.prop, self.client.get("/profile/").context["user_listings"])

    def test_deleting_listing_without_payments_deletes(self):
        self.client.force_login(self.owner)
        self.client.post(f"/property/{self.prop.pk}/delete/")
        self.assertFalse(Property.objects.filter(pk=self.prop.pk).exists())

    def test_orm_delete_of_paid_listing_is_blocked(self):
        create_payment(self.owner, self.prop)
        with self.assertRaises(ProtectedError):
            self.prop.delete()

    def test_deleting_user_keeps_payments(self):
        payment = create_payment(self.owner, self.prop)
        self.owner.delete()
        payment.refresh_from_db()
        self.assertIsNone(payment.user)
        self.assertEqual(payment.user_snapshot, "owner <owner@example.com>")
        self.prop.refresh_from_db()
        self.assertIsNone(self.prop.owner)

    def test_db_constraints(self):
        cases = [dict(price=-1), dict(rooms=0), dict(area_sqm=0), dict(mobility_level=9),
                 dict(floor=5, total_floors=3)]
        for extra in cases:
            with self.assertRaises(IntegrityError, msg=extra), transaction.atomic():
                make_property(owner=self.owner, **extra)

    def test_new_property_defaults_are_safe(self):
        p = Property.objects.create(name="x", address="y")
        self.assertFalse(p.is_published)
        self.assertEqual(p.moderation_status, ModerationStatus.PENDING)
        self.assertFalse(Property.objects.public().filter(pk=p.pk).exists())

    def test_integrity_command_is_read_only(self):
        make_property(owner=self.owner, is_featured=True, featured_until=timezone.now() + timedelta(days=3))
        before = list(Property.objects.values_list("pk", "updated_at"))
        out = StringIO()
        call_command("check_data_integrity", stdout=out)
        self.assertIn("Блокерів міграції не знайдено", out.getvalue())
        self.assertIn("[1] Чинний ТОП", out.getvalue())
        self.assertEqual(before, list(Property.objects.values_list("pk", "updated_at")))


class LegacyTopTests(TestCase):
    def test_top_no_longer_sold_but_existing_top_kept(self):
        owner = make_user("owner")
        until = timezone.now() + timedelta(days=5)
        prop = make_property(owner=owner, is_featured=True, featured_until=until)
        self.client.force_login(owner)
        resp = self.client.post(f"/property/{prop.pk}/promote/")
        self.assertRedirects(resp, f"/property/{prop.pk}/", fetch_redirect_response=False)
        prop.refresh_from_db()
        self.assertEqual((prop.is_featured, prop.featured_until), (True, until))
        detail = self.client.get(f"/property/{prop.pk}/").content.decode()
        self.assertNotIn("Підняти у ТОП", detail)
        self.assertIn("ТОП", self.client.get("/catalog/").content.decode())

    def test_payment_page_shows_placement_tariff(self):
        owner = make_user("owner")
        prop = make_property(owner=owner, public=False)
        self.client.force_login(owner)
        html = self.client.get(f"/payment/{prop.pk}/").content.decode()
        # Вимога замовника: публікація безкоштовна; оплата розміщення 99/30 — лише при обов'язковій оплаті.
        self.assertIn("Безкоштовна публікація", html)
        self.assertNotIn("Розміщення квартири", html)
        with self.settings(PLACEMENT_PAYMENT_REQUIRED=True):
            required = self.client.get(f"/payment/{prop.pk}/").content.decode()
        self.assertIn("Розміщення квартири", required)
        self.assertIn("99 грн", required)
        # Нові тарифи просування продаються через LiqPay (старий ТОП з is_featured — ні).
        self.assertIn("ТОП №1", html)
        self.assertNotIn("Підняти у ТОП", html)


class LogoutTests(TestCase):
    def test_get_does_not_logout(self):
        self.client.force_login(make_user("u"))
        resp = self.client.get("/logout/")
        self.assertEqual(resp.status_code, 200)
        self.assertIsNotNone(self.client.session.get("_auth_user_id"))

    def test_post_logs_out(self):
        self.client.force_login(make_user("u"))
        resp = self.client.post("/logout/")
        self.assertRedirects(resp, "/", fetch_redirect_response=False)
        self.assertIsNone(self.client.session.get("_auth_user_id"))

    def test_logout_requires_csrf(self):
        from django.test import Client

        c = Client(enforce_csrf_checks=True)
        c.force_login(make_user("u"))
        self.assertEqual(c.post("/logout/").status_code, 403)
        self.assertIsNotNone(c.session.get("_auth_user_id"))


class AdminPlacementTests(TestCase):
    def setUp(self):
        self.admin = make_admin()
        self.client.force_login(self.admin)
        self.owner = make_user("owner")
        self.prop = make_property(owner=self.owner)
        self.payment = create_payment(self.owner, self.prop)
        _pay(self.payment, txn="T-9")

    def test_payment_admin_is_view_only(self):
        self.assertEqual(self.client.get("/admin/properties/payment/").status_code, 200)
        url = f"/admin/properties/payment/{self.payment.pk}/change/"
        self.assertEqual(self.client.get(url).status_code, 200)
        self.client.post(url, {"status": "failure", "amount": "1"})
        self.payment.refresh_from_db()
        self.assertEqual(self.payment.status, PaymentStatus.SUCCESS)
        self.assertEqual(self.client.get("/admin/properties/payment/add/").status_code, 403)
        self.assertEqual(self.client.get(f"/admin/properties/payment/{self.payment.pk}/delete/").status_code, 403)

    def test_payment_search_and_filter(self):
        resp = self.client.get("/admin/properties/payment/?q=T-9")
        self.assertContains(resp, self.payment.order_id)
        resp = self.client.get("/admin/properties/payment/?status__exact=success")
        self.assertContains(resp, self.payment.order_id)

    def test_tariff_admin_edit(self):
        t = Tariff.placement()
        resp = self.client.post(f"/admin/properties/tariff/{t.pk}/change/", {
            "name": "Розміщення квартири", "price": "109.00", "currency": "UAH",
            "duration_days": "30", "is_active": "on",
        })
        self.assertEqual(resp.status_code, 302)
        t.refresh_from_db()
        self.payment.refresh_from_db()
        self.assertEqual(t.price, Decimal("109.00"))
        self.assertEqual(self.payment.amount, Decimal("99.00"))

    def test_property_admin_filters_actions_and_detail(self):
        for q in ("placement=active", "placement=never", "placement=expired", "archived=yes", "archived=no", "q=Квартира"):
            self.assertEqual(self.client.get(f"/admin/properties/property/?{q}").status_code, 200, q)
        detail = self.client.get(f"/admin/properties/property/{self.prop.pk}/change/")
        self.assertContains(detail, self.payment.order_id)
        self.client.post("/admin/properties/property/", {"action": "deactivate_listings", "_selected_action": [self.prop.pk]})
        self.prop.refresh_from_db()
        self.assertFalse(self.prop.is_published)
        self.client.post("/admin/properties/property/", {"action": "archive_listings", "_selected_action": [self.prop.pk]})
        self.prop.refresh_from_db()
        self.assertIsNotNone(self.prop.archived_at)

    def test_paid_listing_admin_delete_is_refused(self):
        resp = self.client.post(f"/admin/properties/property/{self.prop.pk}/delete/", {"post": "yes"})
        self.assertEqual(resp.status_code, 200)  # сторінка «неможливо видалити: захищені об'єкти»
        self.assertTrue(Property.objects.filter(pk=self.prop.pk).exists())

    def test_user_admin(self):
        self.assertEqual(self.client.get("/admin/auth/user/").status_code, 200)
        resp = self.client.get(f"/admin/auth/user/{self.owner.pk}/change/")
        self.assertContains(resp, self.payment.order_id)
        self.client.post("/admin/auth/user/", {"action": "deactivate_users",
                                               "_selected_action": [self.owner.pk, self.admin.pk]})
        self.owner.refresh_from_db()
        self.admin.refresh_from_db()
        self.assertFalse(self.owner.is_active)
        self.assertTrue(self.admin.is_active)  # себе деактивувати не можна

    def test_staff_without_superuser_cannot_deactivate_superuser(self):
        staff = User.objects.create_user("mod", "m@example.com", "x", is_staff=True)
        from django.contrib.auth.models import Permission
        staff.user_permissions.add(*Permission.objects.filter(codename__in=["change_user", "view_user"]))
        self.client.force_login(staff)
        self.client.post("/admin/auth/user/", {"action": "deactivate_users", "_selected_action": [self.admin.pk]})
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.is_active)


class TransitionPeriodTests(TestCase):
    def setUp(self):
        self.owner = make_user("owner")
        self.live = make_property(owner=self.owner, name="Опубліковане")
        self.pending = make_property(owner=self.owner, public=False, name="На модерації")
        self.archived = make_property(owner=self.owner, archived_at=timezone.now())
        self.already_paid = make_property(owner=self.owner,
                                          placement_until=timezone.now() + timedelta(days=3))

    def _state(self):
        return {r["pk"]: r for r in Property.objects.values(
            "pk", "moderation_status", "is_published", "is_verified_by_admin", "updated_at", "created_at")}

    def test_dry_run_changes_nothing(self):
        out = StringIO()
        call_command("apply_placement_transition", stdout=out)
        self.assertIn("Оголошень для перехідного періоду: 1", out.getvalue())
        self.live.refresh_from_db()
        self.assertIsNone(self.live.placement_until)
        self.assertFalse(PlacementTransition.objects.exists())

    def test_apply_once_only_touches_placement(self):
        before = self._state()
        call_command("apply_placement_transition", "--apply", "--start", "2026-12-01", stdout=StringIO())
        self.live.refresh_from_db()
        expected = timezone.make_aware(datetime(2026, 12, 31))
        self.assertEqual(self.live.placement_until, expected)
        for pk in (self.pending.pk, self.archived.pk):
            self.assertIsNone(Property.objects.get(pk=pk).placement_until)
        self.assertGreater(Property.objects.get(pk=self.already_paid.pk).placement_until, timezone.now())
        self.assertEqual(before, self._state())
        record = PlacementTransition.objects.get()
        self.assertEqual((record.listings_count, record.listing_ids), (1, [self.live.pk]))
        with self.assertRaises(PlacementError):
            apply_transition(timezone.now())
        out = StringIO()
        call_command("apply_placement_transition", "--apply", stdout=out)
        self.assertIn("Уже застосовано", out.getvalue())
        self.assertEqual(PlacementTransition.objects.count(), 1)

    @override_settings(PLACEMENT_PAYMENT_REQUIRED=True)
    def test_after_transition_ends_payment_is_required(self):
        apply_transition(timezone.now())
        self.assertTrue(Property.objects.public().filter(pk=self.live.pk).exists())
        Property.objects.filter(pk=self.live.pk).update(placement_until=timezone.now() - timedelta(seconds=1))
        self.assertFalse(Property.objects.public().filter(pk=self.live.pk).exists())
        payment, _ = _pay(create_payment(self.owner, self.live))
        self.live.refresh_from_db()
        self.assertEqual(self.live.moderation_status, ModerationStatus.PENDING)
        self.assertFalse(Property.objects.public().filter(pk=self.live.pk).exists())
        self.live.moderation_status = ModerationStatus.APPROVED
        self.live.save()
        self.assertTrue(Property.objects.public().filter(pk=self.live.pk).exists())
        payment.refresh_from_db()
        self.assertEqual(payment.amount, Decimal("99.00"))
        self.assertEqual(payment.period_end - payment.period_start, timedelta(days=30))
