from flask import g, render_template, request, redirect, url_for, jsonify

from charat2.model.connections import use_db

@use_db
def home():
    return render_template(
        "home.html",
        logged_in=g.user is not None,
    )

@use_db
def feed():
    return "ok"

