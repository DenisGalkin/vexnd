from __future__ import annotations

from flask import flash, redirect, render_template, request, url_for
from flask_login import login_required

from app.bot.common import format_bytes
from app.core.extensions import db
from app.domain.models import PaymentIntent, PromoCode, User
from app.http.helpers import localized_url
from app.services.admin import (
    admin_required,
    ban_user,
    confirm_payment,
    create_or_update_promo,
    dashboard_metrics,
    extend_user_subscription,
    is_admin_user,
    list_payments,
    list_users,
    payment_amount_decimal,
    promo_detail,
    promo_list,
    send_user_bot_message,
    unban_user,
    update_payment_status,
    user_details,
)


def _admin_redirect(tab: str, **params):
    values = {"tab": tab}
    values.update({key: value for key, value in params.items() if value not in (None, "", 0)})
    return redirect(localized_url("admin_panel", **values))


@login_required
@admin_required
def admin_panel():
    tab = (request.args.get("tab") or "dashboard").strip()
    selected_user_id = request.args.get("user", type=int)
    selected_promo_id = request.args.get("promo", type=int)
    user_page = request.args.get("user_page", type=int) or 1
    payment_page = request.args.get("payment_page", type=int) or 1

    users_data = list_users(
        search=(request.args.get("user_search") or "").strip(),
        filter_key=(request.args.get("user_filter") or "all").strip(),
        min_purchases=request.args.get("min_purchases", type=int),
        page=user_page,
    )
    payments_data = list_payments(
        search=(request.args.get("payment_search") or "").strip(),
        status=(request.args.get("payment_status") or "all").strip(),
        page=payment_page,
    )
    selected_user = user_details(selected_user_id) if selected_user_id else None
    selected_promo = promo_detail(selected_promo_id) if selected_promo_id else None
    promos = promo_list()

    return render_template(
        "admin.html",
        admin_tab=tab,
        admin_metrics=dashboard_metrics(),
        users_data=users_data,
        payments_data=payments_data,
        promos=promos,
        selected_user=selected_user,
        selected_promo=selected_promo,
        selected_promo_form=(selected_promo["promo"] if selected_promo else None),
        is_admin_user=is_admin_user,
        payment_amount_decimal=payment_amount_decimal,
        format_bytes=format_bytes,
    )


@login_required
@admin_required
def admin_user_extend(user_id: int):
    user = db.session.get(User, int(user_id))
    days = request.form.get("days", type=int) or 0
    if not user or days <= 0:
        flash("Укажите корректного пользователя и число дней.", "error")
        return _admin_redirect("users", user=user_id)
    try:
        extend_user_subscription(user, days)
        db.session.commit()
        flash("Подписка продлена.", "success")
    except Exception as exc:
        db.session.rollback()
        flash(f"Не удалось продлить подписку: {exc}", "error")
    return _admin_redirect("users", user=user.id)


@login_required
@admin_required
def admin_user_ban(user_id: int):
    user = db.session.get(User, int(user_id))
    if not user:
        flash("Пользователь не найден.", "error")
        return _admin_redirect("users")
    try:
        if user.is_banned:
            unban_user(user)
            flash("Пользователь разблокирован.", "success")
        else:
            ban_user(user, request.form.get("reason"))
            flash("Пользователь заблокирован.", "success")
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        flash(f"Не удалось изменить статус пользователя: {exc}", "error")
    return _admin_redirect("users", user=user.id)


@login_required
@admin_required
def admin_user_message(user_id: int):
    user = db.session.get(User, int(user_id))
    message_text = (request.form.get("message") or "").strip()
    if not user or not message_text:
        flash("Укажите пользователя и текст сообщения.", "error")
        return _admin_redirect("users", user=user_id)
    try:
        send_user_bot_message(user, message_text)
        db.session.commit()
        flash("Сообщение отправлено в Telegram.", "success")
    except Exception as exc:
        db.session.rollback()
        flash(f"Не удалось отправить сообщение: {exc}", "error")
    return _admin_redirect("users", user=user.id)


@login_required
@admin_required
def admin_promo_save():
    try:
        promo = create_or_update_promo(request.form)
        if not promo.code:
            raise ValueError("code_required")
        db.session.commit()
        flash("Промокод сохранен.", "success")
        return _admin_redirect("promos", promo=promo.id)
    except Exception as exc:
        db.session.rollback()
        flash(f"Не удалось сохранить промокод: {exc}", "error")
        return _admin_redirect("promos")


@login_required
@admin_required
def admin_promo_toggle(promo_id: int):
    promo = db.session.get(PromoCode, int(promo_id))
    if not promo:
        flash("Промокод не найден.", "error")
        return _admin_redirect("promos")
    try:
        promo.is_active = not bool(promo.is_active)
        db.session.commit()
        flash("Статус промокода обновлен.", "success")
    except Exception as exc:
        db.session.rollback()
        flash(f"Не удалось обновить статус промокода: {exc}", "error")
    return _admin_redirect("promos", promo=promo.id)


@login_required
@admin_required
def admin_payment_confirm(payment_id: int):
    intent = db.session.get(PaymentIntent, int(payment_id))
    if not intent:
        flash("Платеж не найден.", "error")
        return _admin_redirect("payments")
    try:
        confirm_payment(intent)
        db.session.commit()
        flash("Платеж подтвержден вручную.", "success")
    except Exception as exc:
        db.session.rollback()
        flash(f"Не удалось подтвердить платеж: {exc}", "error")
    return _admin_redirect("payments")


@login_required
@admin_required
def admin_payment_status(payment_id: int):
    intent = db.session.get(PaymentIntent, int(payment_id))
    if not intent:
        flash("Платеж не найден.", "error")
        return _admin_redirect("payments")
    try:
        update_payment_status(
            intent,
            (request.form.get("status") or "").strip(),
            failure_reason=request.form.get("failure_reason"),
        )
        db.session.commit()
        flash("Статус платежа обновлен.", "success")
    except Exception as exc:
        db.session.rollback()
        flash(f"Не удалось обновить статус платежа: {exc}", "error")
    return _admin_redirect("payments")


def register(app) -> None:
    app.add_url_rule("/admin", endpoint="admin_panel", view_func=admin_panel, methods=["GET"])
    app.add_url_rule("/en/admin", endpoint="admin_panel_en", view_func=admin_panel, methods=["GET"])
    app.add_url_rule("/admin/users/<int:user_id>/extend", endpoint="admin_user_extend", view_func=admin_user_extend, methods=["POST"])
    app.add_url_rule("/admin/users/<int:user_id>/ban", endpoint="admin_user_ban", view_func=admin_user_ban, methods=["POST"])
    app.add_url_rule("/admin/users/<int:user_id>/message", endpoint="admin_user_message", view_func=admin_user_message, methods=["POST"])
    app.add_url_rule("/admin/promos/save", endpoint="admin_promo_save", view_func=admin_promo_save, methods=["POST"])
    app.add_url_rule("/admin/promos/<int:promo_id>/toggle", endpoint="admin_promo_toggle", view_func=admin_promo_toggle, methods=["POST"])
    app.add_url_rule("/admin/payments/<int:payment_id>/confirm", endpoint="admin_payment_confirm", view_func=admin_payment_confirm, methods=["POST"])
    app.add_url_rule("/admin/payments/<int:payment_id>/status", endpoint="admin_payment_status", view_func=admin_payment_status, methods=["POST"])
