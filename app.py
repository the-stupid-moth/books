import os
from datetime import datetime
from decimal import Decimal
from dotenv import load_dotenv
from flask import (
    Flask, render_template, redirect, url_for,
    request, flash, session, abort
)
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func
from flask_login import (
    LoginManager, login_user, logout_user,
    login_required, current_user, UserMixin
)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from functools import wraps

# ────────────  базовая конфигурация  ────────────
load_dotenv()
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(
    __name__,
    template_folder=os.path.join(BASE_DIR, "templates"),
    static_folder=os.path.join(BASE_DIR, "static"),
)

app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///" + os.path.join(os.path.dirname(__file__), "buk.db")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["UPLOAD_FOLDER"] = "static/uploads"
app.config["ALLOWED_EXTENSIONS"] = {"png", "jpg", "jpeg", "gif"}
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "dev-secret")

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = "login"

# ────────────  модели  ────────────
class User(UserMixin, db.Model):
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(100), unique=True, nullable=False)
    email = db.Column(db.String(100), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    registration_date = db.Column(db.Date, default=datetime.utcnow)
    status = db.Column(db.Enum("active", "banned", "pending", name="user_status"), default="active")
    is_admin = db.Column(db.Boolean, default=False)

    books = db.relationship("Book", back_populates="owner")
    orders = db.relationship("Order", back_populates="user")

    def set_password(self, pw):
        self.password_hash = generate_password_hash(pw)

    def check_password(self, pw):
        return check_password_hash(self.password_hash, pw)


    @property
    def is_active(self) -> bool:
        """
        Flask-Login использует это свойство, чтобы понять, "активен" ли пользователь.
        Забаненные считаются неактивными.
        """
        return self.status == "active"


class Category(db.Model):
    __tablename__ = "categories"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)

    books = db.relationship("Book", back_populates="category")


class Book(db.Model):
    __tablename__ = "books"
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    author = db.Column(db.String(150), nullable=False)
    year = db.Column(db.Integer)
    description = db.Column(db.Text)
    price = db.Column(db.Numeric(10, 2), nullable=False)
    cover = db.Column(db.String(255))  # путь к изображению
    status = db.Column(db.Enum("отличное", "хорошее", "среднее", "плохое", name="book_condition"), default="хорошее")
    owner_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    category_id = db.Column(db.Integer, db.ForeignKey("categories.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    is_available = db.Column(db.Boolean, nullable=False, default=True)

    owner = db.relationship("User", back_populates="books")
    category = db.relationship("Category", back_populates="books")


class Order(db.Model):
    __tablename__ = "orders"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    creation_date = db.Column(db.Date, default=datetime.utcnow)
    total = db.Column(db.Numeric(10, 2))
    status = db.Column(
        db.Enum("new", "processing", "completed", "cancelled", name="order_status"),
        default="new"
    )
    full_name = db.Column(db.String(255))
    phone = db.Column(db.String(20))
    email = db.Column(db.String(100))
    address = db.Column(db.Text)
    comment = db.Column(db.Text)

    user = db.relationship("User", back_populates="orders")
    items = db.relationship("OrderItem", back_populates="order", cascade="all, delete-orphan")


class OrderItem(db.Model):
    __tablename__ = "order_items"
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey("orders.id"))
    book_id = db.Column(db.Integer, db.ForeignKey("books.id"))
    price_at_time = db.Column(db.Numeric(10, 2))
    quantity = db.Column(db.Integer, default=1)

    order = db.relationship("Order", back_populates="items")
    book = db.relationship("Book")

# ────────────  util  ────────────
@login_manager.user_loader
def load_user(uid): return db.session.get(User, int(uid))

def allowed_file(fname):
    return "." in fname and fname.rsplit(".", 1)[1].lower() in app.config["ALLOWED_EXTENSIONS"]

def cart_count(): return len(session.get("cart", []))
app.jinja_env.globals["cart_count"] = cart_count   # для бейджика в навигации

def admin_required(view_func):
    @wraps(view_func)
    @login_required
    def wrapped(*args, **kwargs):
        if not getattr(current_user, "is_admin", False):
            abort(403)
        return view_func(*args, **kwargs)
    return wrapped

def recalc_order_total(order: Order) -> None:
    """
    Пересчитать сумму заказа по таблице order_items.
    """
    total = (
        db.session.query(
            func.coalesce(
                func.sum(OrderItem.price_at_time * OrderItem.quantity),
                0
            )
        )
        .filter(OrderItem.order_id == order.id)
        .scalar()
    )

    # total приходит как Decimal или int, приводим к Decimal с 2 знаками
    order.total = Decimal(str(total)).quantize(Decimal("0.01"))

# ────────────  маршруты  ────────────
@app.route("/")
def index(): return redirect(url_for("books"))

# ----------  каталог + фильтр  ----------
@app.route("/books")
def books():
    # Показываем только доступные книги
    query = Book.query.filter_by(is_available=True)

    search   = (request.args.get("q") or "").strip()
    genre_id = request.args.get("genre_id", type=int)
    author   = (request.args.get("author") or "").strip()
    min_p    = (request.args.get("min_price") or "").strip()
    max_p    = (request.args.get("max_price") or "").strip()

    # Поиск по названию / автору
    if search:
        like = f"%{search.lower()}%"
        query = query.filter(
            db.or_(
                db.func.lower(Book.title).like(like),
                db.func.lower(Book.author).like(like)
            )
        )

    # Фильтр по жанру
    if genre_id:
        query = query.join(Category).filter(Category.id == genre_id)

    # Фильтр по автору
    if author:
        query = query.filter(Book.author.ilike(f"%{author}%"))

    # Фильтр по цене (ГРАНИЦЫ ВКЛЮЧИТЕЛЬНЫЕ)
    if min_p:
        try:
            min_dec = Decimal(min_p.replace(",", "."))
            query = query.filter(Book.price >= min_dec)
        except Exception:
            pass  # если ввели фигню, просто игнорируем фильтр "от"

    if max_p:
        try:
            max_dec = Decimal(max_p.replace(",", "."))
            query = query.filter(Book.price <= max_dec)
        except Exception:
            pass  # игнорируем некорректный "до"

    books_ = query.order_by(Book.created_at.desc()).all()
    categories = Category.query.order_by(Category.name.asc()).all()

    return render_template(
        "books.html",
        books=books_,
        categories=categories,
        selected_genre_id=genre_id
    )

# ----------  регистрация / вход / выход  ----------
@app.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated: return redirect(url_for("books"))
    if request.method == "POST":
        u, e, p = request.form["username"], request.form["email"].lower(), request.form["password"]
        if not (u and e and p): flash("Заполните все поля", "danger"); return render_template("register.html")
        if User.query.filter(db.or_(User.username==u, User.email==e)).first():
            flash("Такой пользователь уже существует", "danger"); return render_template("register.html")
        user = User(username=u, email=e); user.set_password(p); db.session.add(user); db.session.commit()
        flash("Теперь можно войти", "success"); return redirect(url_for("login"))
    return render_template("register.html")

@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("books"))

    if request.method == "POST":
        username = request.form["username"].strip()
        password = request.form["password"]
        remember = bool(request.form.get("remember"))

        user = User.query.filter_by(username=username).first()

        if user and user.check_password(password):
            if user.status == "banned":
                flash("Ваш аккаунт заблокирован администратором.", "danger")
                return render_template("login.html")

            if user.status == "pending":
                flash("Ваш аккаунт ещё не активирован.", "warning")
                return render_template("login.html")

            login_user(user, remember=remember)
            flash("Добро пожаловать!", "success")
            return redirect(request.args.get("next") or url_for("books"))

        flash("Неверный логин/пароль", "danger")

    return render_template("login.html")



@app.route("/logout")
@login_required
def logout(): logout_user(); flash("Вы вышли", "info"); return redirect(url_for("login"))

# ----------  CRUD книг  ----------
@app.route("/my_books")
@login_required
def my_books():
    return render_template("my_books.html", books=current_user.books)

@app.route("/book/new", methods=["GET", "POST"])
@login_required
def book_create():
    return _book_form()

@app.route("/book/<int:book_id>/edit", methods=["GET", "POST"])
@login_required
def book_edit(book_id):
    book = Book.query.get_or_404(book_id)
    if book.owner_id != current_user.id: abort(403)
    return _book_form(book)

@app.route("/book/<int:book_id>")
def book_detail(book_id):
    book = Book.query.get_or_404(book_id)
    return render_template("book_detail.html", book=book)


@app.route("/book/<int:book_id>/delete", methods=["POST"])
@login_required
def book_delete(book_id):
    book = Book.query.get_or_404(book_id)
    if not (current_user.is_admin or book.owner_id == current_user.id):
        abort(403)

    db.session.delete(book)
    db.session.commit()
    flash("Книга удалена", "info")
    return redirect(url_for("my_books"))

def _book_form(book: Book | None = None):
    """
    Обработчик создания/редактирования книги.
    Если book == None  → создание, иначе редактирование.
    """
    categories = Category.query.order_by(Category.name.asc()).all()

    if request.method == "POST":
        f = request.form
        title, author = f["title"].strip(), f["author"].strip()
        if not (title and author):
            flash("Заполните обязательные поля: Название и Автор", "danger")
            return redirect(request.url)

        if not book:
            book = Book(owner_id=current_user.id)

        book.title = title
        book.author = author
        book.description = f.get("description", "").strip() or None

        # существующая категория из select
        category_id = f.get("category_id")
        new_category_name = f.get("new_category", "").strip()

        if new_category_name:
            # если введён новый жанр – создаём Category
            cat = Category.query.filter(
                db.func.lower(Category.name) == new_category_name.lower()
            ).first()
            if not cat:
                cat = Category(name=new_category_name)
                db.session.add(cat)
                db.session.flush()
            book.category = cat
        else:
            # иначе берём выбранный в селекте
            if category_id:
                cat = Category.query.get(int(category_id))
                book.category = cat
            else:
                book.category = None

        # год
        year_raw = f.get("year", "").strip()
        if year_raw:
            try:
                book.year = int(year_raw)
            except ValueError:
                flash("Год издания должен быть числом", "danger")
                return redirect(request.url)
        else:
            book.year = None
        # цена
        price_raw = f["price"].replace(",", ".")
        try:
            book.price = Decimal(price_raw)
        except Exception:
            flash("Некорректное значение цены", "danger")
            return redirect(request.url)

        book.status = f.get("status", "хорошее")

        # обновить/загрузить картинку
        file = request.files.get("photo")
        if file and allowed_file(file.filename):
            fname = secure_filename(f"{datetime.utcnow().timestamp()}_{file.filename}")
            os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
            file.save(os.path.join(app.config["UPLOAD_FOLDER"], fname))
            book.cover = fname   # поле cover в модели Book

        db.session.add(book)
        db.session.commit()
        flash("Книга сохранена", "success")
        return redirect(url_for("my_books"))

    return render_template("book_form.html", book=book, categories=categories)

# ----------  корзина  ----------
@app.route("/add_to_cart/<int:book_id>", methods=["POST"])
@login_required
def add_to_cart(book_id):
    book = Book.query.get_or_404(book_id)

    # Если книга уже недоступна (кто-то купил) – не даём добавить
    if not book.is_available:
        flash("Эта книга уже куплена другим пользователем.", "warning")
        return redirect(request.referrer or url_for("books"))

    session.setdefault("cart", [])
    if book_id not in session["cart"]:
        session["cart"].append(book_id)
        session.modified = True

    flash("Книга добавлена в корзину", "success")
    return redirect(request.referrer or url_for("books"))



@app.route("/cart")
@login_required
def cart():
    cart_ids = session.get("cart", [])
    books = Book.query.filter(Book.id.in_(cart_ids)).all() if cart_ids else []
    total = sum(b.price for b in books)
    return render_template("cart.html", books=books, total=total)

@app.route("/cart/remove/<int:book_id>", methods=["POST"])
@login_required
def cart_remove(book_id):
    session["cart"] = [bid for bid in session.get("cart", []) if bid != book_id]
    session.modified = True
    return redirect(url_for("cart"))

@app.route("/cart/checkout", methods=["GET", "POST"])
@login_required
def cart_checkout():
    cart_ids = session.get("cart", [])
    if not cart_ids:
        flash("Корзина пуста", "warning")
        return redirect(url_for("books"))

    if request.method == "GET":
        # форма теперь на странице /cart
        return redirect(url_for("cart"))

    books = Book.query.filter(Book.id.in_(cart_ids)).all()
    if not books:
        flash("Корзина пуста", "warning")
        return redirect(url_for("books"))

    # POST – обработка формы
    f = request.form
    full_name = f.get("full_name", "").strip()
    phone = f.get("phone", "").strip()
    address = f.get("address", "").strip()
    email = f.get("email", "").strip()
    comment = f.get("comment", "").strip()

    if not (full_name and phone and address):
        flash("Заполните обязательные поля: ФИО, телефон и адрес", "danger")
        return redirect(url_for("cart"))

    order = Order(
        user_id=current_user.id,
        creation_date=datetime.utcnow().date(),
        full_name=full_name,
        phone=phone,
        address=address,
        email=email or None,
        comment=comment or None,
        status="new",
    )
    db.session.add(order)
    db.session.flush()  # чтобы получить order.id

    total = Decimal("0.00")
    for b in books:
        db.session.add(
            OrderItem(
                order_id=order.id,
                book_id=b.id,
                price_at_time=b.price,
                quantity=1,
            )
        )
        total += b.price

        # книга оформлена – скрываем из каталога
        b.is_available = False

    order.total = total
    db.session.commit()


    session.pop("cart", None)
    flash("Заказ оформлен!", "success")
    return redirect(url_for("orders"))

# ----------  история заказов  ----------
@app.route("/orders")
@login_required
def orders():
    orders_ = (
        Order.query
        .filter(
            Order.user_id == current_user.id,
            Order.status != "cancelled"   # не показываем отменённые
        )
        .order_by(Order.creation_date.desc())
        .all()
    )
    return render_template("orders.html", orders=orders_)

# # ────────────  админка  ────────────

@app.route("/admin")
@admin_required
def admin_dashboard():
    users = User.query.order_by(User.id.asc()).all()
    books = Book.query.order_by(Book.created_at.desc()).all()
    orders_ = Order.query.order_by(Order.creation_date.desc()).all()
    return render_template(
        "admin_dashboard.html",
        users=users,
        books=books,
        orders=orders_
    )

@app.route("/admin/users/<int:user_id>/status", methods=["POST"])
@admin_required
def admin_set_user_status(user_id):
    user = User.query.get_or_404(user_id)
    status = request.form.get("status")
    if status not in ("active", "banned", "pending"):
        abort(400)
    user.status = status
    db.session.commit()
    flash("Статус пользователя обновлён", "success")
    return redirect(url_for("admin_dashboard"))

@app.route("/orders/<int:order_id>/edit", methods=["GET", "POST"])
@login_required
def order_edit(order_id):
    order = Order.query.get_or_404(order_id)

    # Проверка прав
    if order.user_id != current_user.id and not current_user.is_admin:
        abort(403)

    # Нельзя редактировать завершённые и отменённые заказы
    if order.status in ("completed", "cancelled"):
        flash("Этот заказ нельзя редактировать", "warning")
        return redirect(url_for("orders") if not current_user.is_admin
                        else url_for("admin_dashboard"))

    if request.method == "POST":
        f = request.form

        # ---------- обновление данных заказа ----------
        full_name = f.get("full_name", "").strip()
        phone = f.get("phone", "").strip()
        address = f.get("address", "").strip()
        email = f.get("email", "").strip()
        comment = f.get("comment", "").strip()

        if not (full_name and phone and address):
            flash("Заполните обязательные поля: ФИО, телефон и адрес", "danger")
            return render_template("order_edit.html", order=order)

        order.full_name = full_name
        order.phone = phone
        order.address = address
        order.email = email or None
        order.comment = comment or None

        # ---------- обработка редактирования списка книг ----------
        keep_ids = [int(bid) for bid in f.getlist("books")]

        # Удаляем те OrderItem, книг которых нет в "оставленных"
        for item in list(order.items):
            if item.book_id not in keep_ids:
                db.session.delete(item)

        # Обновляем БД и пересчитываем сумму
        db.session.flush()
        recalc_order_total(order)       

        db.session.commit()
        flash("Заказ обновлён", "success")

        return redirect(url_for("orders") if not current_user.is_admin
                        else url_for("admin_dashboard"))

    return render_template("order_edit.html", order=order)


@app.route("/orders/<int:order_id>/cancel", methods=["POST"])
@login_required
def order_cancel(order_id):
    order = Order.query.get_or_404(order_id)
    if order.user_id != current_user.id and not current_user.is_admin:
        abort(403)

    if order.status in ("completed", "cancelled"):
        flash("Этот заказ нельзя отменить", "warning")
    else:
        # вернуть все книги из заказа в каталог
        for item in order.items:
            if hasattr(item.book, "is_available"):
                item.book.is_available = True

        order.status = "cancelled"
        db.session.commit()
        flash("Заказ отменён", "info")

    return redirect(url_for("orders") if not current_user.is_admin else url_for("admin_dashboard"))

@app.route("/orders/<int:order_id>/items/<int:item_id>/delete", methods=["POST"])
@login_required
def order_item_delete(order_id, item_id):
    order = Order.query.get_or_404(order_id)

    # проверка прав
    if order.user_id != current_user.id and not current_user.is_admin:
        abort(403)

    item = (
        OrderItem.query
        .filter_by(id=item_id, order_id=order.id)
        .first_or_404()
    )

    # вернуть книгу в каталог (если используешь is_available)
    if hasattr(item.book, "is_available"):
        item.book.is_available = True

    # удаляем позицию
    db.session.delete(item)

    # пересчитываем сумму заказа
    recalc_order_total(order)

    # если в заказе больше нет книг — помечаем его отменённым
    if len(order.items) == 0:
        order.status = "cancelled"

    db.session.commit()

    flash("Книга удалена из заказа", "info")
    return redirect(
        url_for("orders") if not current_user.is_admin
        else url_for("admin_dashboard")
    )
@app.route("/orders/<int:order_id>/delete", methods=["POST"])
@login_required
def order_delete(order_id):
    order = Order.query.get_or_404(order_id)

    # проверка прав
    if order.user_id != current_user.id and not current_user.is_admin:
        abort(403)

    # вернуть книги в каталог, если используешь is_available
    for item in order.items:
        if hasattr(item.book, "is_available"):
            item.book.is_available = True

    # удалить сам заказ
    db.session.delete(order)
    db.session.commit()

    flash("Заказ удалён", "info")
    return redirect(
        url_for("orders") if not current_user.is_admin
        else url_for("admin_dashboard")
    )


@app.route("/admin/orders/<int:order_id>/status", methods=["POST"])
@admin_required
def admin_set_order_status(order_id):
    order = Order.query.get_or_404(order_id)
    status = request.form.get("status")

    allowed_statuses = ("new", "processing", "completed", "cancelled")
    if status not in allowed_statuses:
        abort(400)

    order.status = status
    db.session.commit()
    flash("Статус заказа обновлён", "success")
    return redirect(url_for("admin_dashboard"))




# ────────────  точка входа  ────────────
if __name__ == "__main__":
    with app.app_context():
        db.create_all()
        if Category.query.count() == 0:
            base_categories = [
                "Фантастика",
                "Фэнтези",
                "Детектив",
                "Роман",
                "Классика",
                "Научная литература",
                "Учебная литература",
                "Детская литература",
                "Поэзия"
            ]
            for name in base_categories:
                db.session.add(Category(name=name))
            db.session.commit()
    app.run(debug=True)