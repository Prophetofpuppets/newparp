from flask import g, render_template

from charat2.model.connections import use_db

@use_db
def home():
    if g.user is not None:
        return render_template("home.html")
    else:
        return render_template("register.html")

