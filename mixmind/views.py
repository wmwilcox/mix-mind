"""
Application main for the mixmind app
"""
import os
import random
import datetime
import tempfile
import urllib

from flask import g, render_template, flash, request, send_file, jsonify, redirect, url_for
from flask_security import login_required, roles_required
from flask_security.decorators import _get_unauthorized_view
from flask_login import current_user

from .notifier import send_mail
from .forms import DrinksForm, OrderForm, OrderFormAnon, RecipeForm, RecipeListSelector, BarstockForm, UploadBarstockForm, LoginForm, CreateBarForm, EditBarForm, EditUserForm
from .authorization import user_datastore
from .barstock import Barstock_SQL, Ingredient, _update_computed_fields
from .formatted_menu import filename_from_options, generate_recipes_pdf
from .compose_html import recipe_as_html, users_as_table, orders_as_table, bars_as_table
from .util import filter_recipes, DisplayOptions, FilterOptions, PdfOptions, load_recipe_json, report_stats, find_recipe, convert_units
from .database import db
from .models import User, Order, Bar
from . import log, app, mms, current_bar

"""
BUGS:
NOTES:
* cards should be same sizes
* admin pages
    - raise 404 on not authorized
    - add/remove ingredients dynamically?
        - using jQuery, ajax and datatables. And the editable datatable plugin
    - add/remove recipes as raw json
        - ace embeddable text editor
    - menu_generator (what's now "mainpage")
* better commits to db with after_this_request
* menu schemas
    - would be able to include definitive item lists for serving, ice, tag, etc.
* hardening
    - get logging working for reals
    - test error handling
* configuration management
    - better support for declaring someone a bartener
        - or better yet, removing them as a bartender and keeping implicit declared
* "remember" form open/close position of collapses
    - use util to unshow the filters button??
* computer modern typerwriter font for recipes
"""
@app.before_request
def initialize_shared_data():
    g.bar_id = current_bar.id

def get_form(form_class):
    """WTForms update 2.2 breaks when an empty request.form
    is given to it """
    if not request.form:
        return form_class()
    return form_class(request.form)

def bundle_options(tuple_class, args):
    return tuple_class(*(getattr(args, field).data for field in tuple_class._fields))

def recipes_from_options(form, display_opts=None, filter_opts=None, to_html=False, order_link=False, convert_to=None, **kwargs_for_html):
    """ Apply display formmatting, filtering, sorting to
    the currently loaded recipes.
    Also can generate stats
    May convert to html, including extra options for that
    Apply sorting
    """
    display_options = bundle_options(DisplayOptions, form) if not display_opts else display_opts
    filter_options = bundle_options(FilterOptions, form) if not filter_opts else filter_opts
    recipes, excluded = filter_recipes(mms.recipes, filter_options, union_results=bool(filter_options.search))
    if form.sorting.data and form.sorting.data != 'None': # TODO this is weird
        reverse = 'X' in form.sorting.data
        attr = 'avg_{}'.format(form.sorting.data.rstrip('X'))
        recipes = sorted(recipes, key=lambda r: getattr(r.stats, attr), reverse=reverse)
    if convert_to:
        map(lambda r: r.convert(convert_to), recipes)
    if display_options.stats and recipes:
        stats = report_stats(recipes, as_html=True)
    else:
        stats = None
    if to_html:
        if order_link:
            recipes = [recipe_as_html(recipe, display_options,
                order_link="/order/{}".format(urllib.quote_plus(recipe.name)),
                **kwargs_for_html) for recipe in recipes]
        else:
            recipes = [recipe_as_html(recipe, display_options, **kwargs_for_html) for recipe in recipes]
    return recipes, excluded, stats


################################################################################
# Customer routes
################################################################################


@app.route("/", methods=['GET', 'POST'])
def browse():
    form = get_form(DrinksForm)
    filter_options = None

    if request.method == 'GET':
        # filter for current recipes that can be made on the core list
        filter_options = FilterOptions(search="",all_=False,include="",exclude="",include_use_or=False,exclude_use_or=False,style="",glass="",prep="",ice="",name="",tag="core")

    display_opts = DisplayOptions(
                        prices=current_bar.prices,
                        stats=False,
                        examples=current_bar.examples,
                        all_ingredients=False,
                        markup=current_bar.markup,
                        prep_line=current_bar.prep_line,
                        origin=current_bar.origin,
                        info=current_bar.info,
                        variants=current_bar.variants)
    recipes, _, _ = recipes_from_options(form, display_opts=display_opts, filter_opts=filter_options,
                            to_html=True, order_link=True, convert_to=current_bar.convert, condense_ingredients=current_bar.summarize)

    if request.method == 'POST':
        if form.validate():
            n_results = len(recipes)
            if n_results > 0:
                if 'suprise-menu' in request.form:
                    recipes = [random.choice(recipes)]
                    flash(u"Bartender's choice! Just try again if you want something else!")
                else:
                    flash(u"Filters applied. Showing {} available recipes".format(n_results), 'success')
            else:
                flash(u"No results after filtering, try being less specific", 'warning')
        else:
            flash(u"Error in form validation", 'danger')

    return render_template('browse.html', form=form, recipes=recipes)

@app.route("/order/<recipe_name>", methods=['GET', 'POST'])
def order(recipe_name):
    if current_user.is_authenticated:
        form = get_form(OrderForm)
    else:
        form = get_form(OrderFormAnon)

    recipe_name = urllib.unquote_plus(recipe_name)
    show_form = False
    heading = u"Order:"

    recipe = find_recipe(mms.recipes, recipe_name)
    recipe.convert(u'oz')
    if not recipe:
        flash(u'Error: unknown recipe "{}"'.format(recipe_name), 'danger')
        return render_template('result.html', heading=heading)
    else:
        recipe_html = recipe_as_html(recipe, DisplayOptions(
                            prices=current_bar.prices,
                            stats=False,
                            examples=True,
                            all_ingredients=False,
                            markup=current_bar.markup,
                            prep_line=True,
                            origin=current_bar.origin,
                            info=True,
                            variants=True), convert_to=current_bar.convert)

    if not recipe.can_make:
        flash(u'Ingredients to make this are out of stock :(', 'warning')
        return render_template('order.html', form=form, recipe=recipe_html, show_form=False)

    if request.method == 'GET':
        show_form = True
        if current_user.is_authenticated:
            heading = "Order for {}:".format(current_user.get_name(short=True))
        if current_bar.is_closed:
            flash(u"It's closed. So sad.", 'warning')

    if request.method == 'POST':
        if 'submit-order' in request.form:
            if form.validate():
                if current_user.is_authenticated:
                    user_name = current_user.get_name()
                    user_email = current_user.email
                else:
                    user_name = form.name.data
                    user_email = form.email.data

                if current_bar.is_closed:
                    flash(u'The bar is currently closed for orders.', 'warning')
                    return redirect(request.url)

                # use simpler html for recording an order
                email_recipe_html = recipe_as_html(recipe, DisplayOptions(
                                    prices=current_bar.prices,
                                    stats=False,
                                    examples=True,
                                    all_ingredients=False,
                                    markup=current_bar.markup,
                                    prep_line=True,
                                    origin=current_bar.origin,
                                    info=True,
                                    variants=True), fancy=False, convert_to=current_bar.convert)

                # add to the order database
                order = Order(bar_id=current_bar.id, bartender_id=current_bar.bartender.id,
                        timestamp=datetime.datetime.utcnow(),
                        recipe_name=recipe.name, recipe_html=email_recipe_html)
                if current_user.is_authenticated:
                    order.user_id = current_user.id
                db.session.add(order)
                db.session.commit()

                # TODO add a verifiable token to this
                subject = "{} for {} at {}".format(recipe.name, user_name, current_bar.name)
                confirmation_link = "https://{}{}".format(request.host,
                        url_for('confirm_order',
                            email=urllib.quote(user_email),
                            order_id=order.id))
                send_mail(subject, current_bar.bartender.email, "order_submitted",
                        confirmation_link=confirmation_link,
                        name=user_name,
                        notes=form.notes.data,
                        recipe_html=email_recipe_html)

                flash(u"Your order has been submitted, and you'll receive a confirmation email once the bartender acknowledges it", 'success')
                if not current_user.is_authenticated:
                    if User.query.filter_by(email=user_email).one_or_none():
                        flash(u"Hey, if you log in you won't have to keep typing your email address for orders ;)", 'secondary')
                        return redirect(url_for('security.login'))
                    else:
                        flash(u"Hey, if you register I'll remember your name and email in future orders!", 'secondary')
                        return redirect(url_for('security.register'))
                return render_template('result.html', heading="Order Placed")
            else:
                flash(u"Error in form validation", 'danger')

    # either provide the recipe and the form,
    # or after the post show the result
    return render_template('order.html', form=form, recipe=recipe_html, heading=heading, show_form=show_form)

@app.route('/confirm_order')
def confirm_order():
    # TODO this needs a security token
    # TODO this also needs to handle race conditions when switching bars......
    email = urllib.unquote(request.args.get('email'))
    order_id = request.args.get('order_id')
    order = Order.query.filter_by(id=order_id).one_or_none()
    if not order:
        flash(u"Error: Invalid order_id", 'danger')
        return render_template("result.html", heading="Invalid confirmation link")
    if order.confirmed:
        flash(u"Error: Order has already been confirmed", 'danger')
        return render_template("result.html", heading="Invalid confirmation link")

    bartender = user_datastore.find_user(id=order.bartender_id)
    if bartender and bartender.venmo_id:
        venmo_link = app.config.get('VENMO_LINK','').format(bartender.venmo_id)
    else:
        venmo_link = None

    order.confirmed = datetime.datetime.utcnow()

    # update users db
    user = User.query.filter_by(email=email).one_or_none()
    if user:
        greeting = "{}, you".format(user.get_name(short=True))
        if order.user_id and order.user_id != user.id:
            raise ValueError("Order was created with different id than confirming user!")
        user.orders.append(order)
        user_datastore.put(user)
        user_datastore.commit()
    else:
        greeting = "You"

    try:
        subject = "[Mix-Mind] Your {} Confirmation".format(current_bar.name)
        send_mail(subject, email, "order_confirmation",
                greeting=greeting,
                recipe_name=order.recipe_name,
                recipe_html=order.recipe_html,
                venmo_link=venmo_link)
    except Exception:
        log.error("Error sending confirmation email for {} to {}".format(recipe, email))

    flash(u'Confirmation sent')
    return render_template('result.html', heading="Order Confirmation")


@app.route('/user', methods=['GET', 'POST'])
@login_required
def user_profile():
    try:
        user_id = int(request.args.get('user_id'))
    except ValueError:
        flash(u"Invalid user_id parameter", 'danger')
        return render_template('result.html', "User profile unavailable")

    if current_user.id != user_id and not current_user.has_role('admin'):
        return _get_unauthorized_view()

    # leaving this trigger here because it's convenient
    if current_user.email in app.config.get('MAKE_ADMIN', []):
        if not current_user.has_role('admin'):
            admin = user_datastore.find_role('admin')
            user_datastore.add_role_to_user(current_user, admin)
            user_datastore.commit()
            flash(u"You have been upgraded to admin", 'success')

    this_user = user_datastore.find_user(id=user_id)
    if not this_user:
        flash(u"Unknown user_id", 'danger')
        return render_template('result.html', "User profile unavailable")

    form = get_form(EditUserForm)
    if request.method == 'POST':
        if form.validate():
            this_user.first_name = form.first_name.data
            this_user.last_name = form.last_name.data
            this_user.nickname = form.nickname.data
            this_user.venmo_id = form.venmo_id.data
            user_datastore.commit()
            flash(u"Profile updated", 'success')
            return redirect(request.url)
        else:
            flash(u"Error in form validation", 'danger')
            return render_template('user_profile.html', this_user=this_user, edit_user=form,
                    human_timestamp=mms.time_human_formatter, human_timediff=mms.time_diff_formatter,
                    timestamp=mms.timestamp_formatter)



    # TODO make admins able to edit user page
    # pre-populate the form with the current values
    for attr in 'first_name,last_name,nickname,venmo_id'.split(','):
        setattr(getattr(form, attr), 'data', getattr(this_user, attr))

    return render_template('user_profile.html', this_user=this_user, edit_user=form,
                human_timestamp=mms.time_human_formatter, human_timediff=mms.time_diff_formatter,
                timestamp=mms.timestamp_formatter)


@app.route("/user_post_login", methods=['GET'])
def post_login_redirect():
    # show main if admin user
    # maybe use post-register for assigning a role
    if 'admin' in current_user.roles:
        return redirect(url_for('browse'))
    else:
        return redirect(url_for('browse'))


@app.route('/user_post_confirm_email')
@login_required
def user_confirmation_hook():
    if not current_user.has_role('customer'):
        customer = user_datastore.find_role('customer')
        user_datastore.add_role_to_user(current_user, customer)
        user_datastore.commit()
    return render_template('result.html', heading="Email confirmed")


################################################################################
# Admin routes
################################################################################

@app.route("/admin/dashboard", methods=['GET', 'POST'])
@login_required
@roles_required('admin')
def admin_dashboard():
    BAR_BULK_ATTRS = 'name,tagline,prices,prep_line,examples,convert,markup,info,origin,variants,summarize'.split(',')
    new_bar_form = get_form(CreateBarForm)
    edit_bar_form = get_form(EditBarForm)
    if request.method == 'POST':
        if 'create_bar' in request.form:
            if new_bar_form.validate():
                if Bar.query.filter_by(cname=new_bar_form.cname.data).one_or_none():
                    flash(u"Bar name already in use", 'warning')
                    return redirect(request.url)
                bar_args = {'cname': new_bar_form.cname.data}
                if new_bar_form.name.data == "":
                    bar_args['name'] = bar_args['cname']
                else:
                    bar_args['name'] = new_bar_form.name.data
                if new_bar_form.tagline.data:
                    bar_args['tagline'] = new_bar_form.tagline.data
                new_bar = Bar(**bar_args)
                db.session.add(new_bar)
                db.session.commit()
                flash(u"Created a new bar", 'success')
            else:
                flash(u"Error in form validation", 'warning')

        elif 'edit_bar' in request.form:
            if edit_bar_form.validate():
                # TODO allow manager to edit this one? use different post url
                bar_id = edit_bar_form.bar_id.data
                bar = Bar.query.filter_by(id=bar_id).one_or_none()
                if bar is None:
                    flash(u"Invalid bar_id: {}".format(edit_bar_form.bar_id.data), 'danger')
                    return redirect(request.url)

                # assign owner
                user = user_datastore.find_user(email=edit_bar_form.owner.data)
                if user and user.id != bar.owner_id:
                    # TODO remove "owner" role if user does not own any more bars
                    owner = user_datastore.find_role('owner')
                    user_datastore.add_role_to_user(user, owner)
                    bar.owner = user
                    # TODO send email to owner!
                    flash(u"{} is now the proud owner of {}".format(user.get_name(), bar.cname))
                elif edit_bar_form.owner.data == '':
                    # remove the owner from this bar
                    flash(u"{} is no longer the owner of {}".format(bar.owner.get_name(), bar.cname))
                    bar.owner = None

                # add bartender on duty
                user = user_datastore.find_user(email=edit_bar_form.bartender.data)
                if user and user.id != bar.bartender_on_duty:
                    bartender = user_datastore.find_role('bartender')
                    user_datastore.add_role_to_user(user, bartender)
                    bar.bartenders.append(user)
                    bar.bartender_on_duty = user.id
                    # TODO send email to bartender on duty
                else:
                    # closed/no bartender is same result
                    if not user or edit_bar_form.status.data == False:
                        bar.bartender_on_duty = None

                for attr in BAR_BULK_ATTRS:
                    setattr(bar, attr, getattr(edit_bar_form, attr).data)
                db.session.commit()
                flash(u"Successfully updated config for {}".format(bar.cname))
                return redirect(request.url)
            else:
                flash(u"Error in form validation", 'warning')

        elif 'activate-bar' in request.form:
            bar_id = request.form.get('bar_id', None, int)
            if bar_id == current_bar.id:
                flash(u"Bar ID: {} is already active".format(bar_id), 'warning')
                return redirect(request.url)
            to_activate_bar = Bar.query.filter_by(id=bar_id).one_or_none()
            if not to_activate_bar:
                flash(u"Error: Bar ID: {} is invalid".format(bar_id), 'danger')
                return redirect(request.url)
            # TODO make this shit atomicer
            bars = Bar.query.all()
            for bar in bars:
                bar.is_active = bar.id == bar_id
            db.session.commit()
            mms.regenerate_recipes(to_activate_bar)
            flash(u"Bar ID: {} is now active".format(bar_id), 'success')
            return redirect(request.url)

    # for GET requests, fill in the edit bar form
    edit_bar_form.bar_id.render_kw['value'] = current_bar.id
    edit_bar_form.status.data = not current_bar.is_closed
    edit_bar_form.bartender.data = '' if current_bar.is_closed else current_bar.bartender.email
    edit_bar_form.owner.data = '' if not current_bar.owner else current_bar.owner.email
    for attr in BAR_BULK_ATTRS:
        setattr(getattr(edit_bar_form, attr), 'data', getattr(current_bar, attr))

    bars = Bar.query.all()
    users = User.query.all()
    orders = Order.query.all()
    #bar_table = bars_as_table(bars)
    user_table = users_as_table(users)
    order_table = orders_as_table(orders)
    return render_template('dashboard.html', new_bar_form=new_bar_form, edit_bar_form=edit_bar_form,
            users=users, orders=orders,
            bars=bars, user_table=user_table, order_table=order_table)

@app.route("/admin/menu_generator", methods=['GET', 'POST'])
@login_required
@roles_required('admin')
def menu_generator():
    form = get_form(DrinksForm)
    print form.errors
    recipes = []
    excluded = None
    stats = None

    if request.method == 'POST':
        if form.validate():
            print request
            recipes, excluded, stats = recipes_from_options(form, to_html=True)
            flash(u"Settings applied. Showing {} available recipes".format(len(recipes)))
        else:
            flash(u"Error in form validation", 'danger')

    return render_template('menu_generator.html', form=form, recipes=recipes, excluded=excluded, stats=stats)


@app.route("/admin/menu_generator/download/", methods=['POST'])
@login_required
@roles_required('admin')
def menu_download():
    form = get_form(DrinksForm)
    raise NotImplementedError

    if form.validate():
        print request
        recipes, _, _ = recipes_from_options(form)

        display_options = bundle_options(DisplayOptions, form)
        form.pdf_filename.data = 'menus/{}'.format(filename_from_options(bundle_options(PdfOptions, form), display_options))
        pdf_options = bundle_options(PdfOptions, form)
        pdf_file = '{}.pdf'.format(pdf_options.pdf_filename)

        generate_recipes_pdf(recipes, pdf_options, display_options, mms.barstock.df)
        return send_file(os.path.abspath(pdf_file), 'application/pdf', as_attachment=True, attachment_filename=pdf_file.lstrip('menus/'))

    else:
        flash(u"Error in form validation", 'danger')
        return render_template('application_main.html', form=form, recipes=[], excluded=None)


@app.route("/admin/recipes", methods=['GET','POST'])
@login_required
@roles_required('admin')
def recipe_library():
    return render_template('result.html', heading="Still under construction...")
    select_form = get_form(RecipeListSelector)
    print select_form.errors
    add_form = get_form(RecipeForm)
    print add_form.errors

    if request.method == 'POST':
        print request
        if 'recipe-list-select' in request.form:
            recipes = select_form.recipes.data
            mms.regenerate_recipes(current_bar)
            flash(u"Now using recipes from {}".format(recipes))

    return render_template('recipes.html', select_form=select_form, add_form=add_form)


@app.route("/admin/ingredients", methods=['GET','POST'])
@login_required
@roles_required('admin')
def ingredient_stock():
    form = get_form(BarstockForm)
    upload_form = get_form(UploadBarstockForm)
    form_open = False
    print form.errors

    if request.method == 'POST':
        print request
        if 'add-ingredient' in request.form:
            if form.validate():
                row = {}
                row['Category'] = form.category.data
                row['Type'] = form.type_.data
                row['Bottle'] = form.bottle.data
                row['ABV'] = float(form.abv.data)
                row['Size (mL)'] = convert_units(float(form.size.data), form.unit.data, 'mL')
                row['Price Paid'] = float(form.price.data)
                ingredient = Barstock_SQL(current_bar.id).add_row(row, current_bar.id)
                mms.regenerate_recipes(current_bar, ingredient=ingredient.type_)
                return redirect(request.url)
            else:
                form_open = True
                flash(u"Error in form validation", 'danger')

        elif 'upload-csv' in request.form:
            # TODO handle files < 500 kb by keeping in mem
            csv_file = request.files['upload_csv']
            if not csv_file or csv_file.filename == '':
                flash(u'No selected file', 'danger')
                return redirect(request.url)
            _, tmp_filename = tempfile.mkstemp()
            csv_file.save(tmp_filename)
            Barstock_SQL(current_bar.id).load_from_csv([tmp_filename], current_bar.id,
                    replace_existing=upload_form.replace_existing.data)
            os.remove(tmp_filename)
            mms.regenerate_recipes(current_bar)
            msg = u"Ingredients database {} {} for bar {}".format(
                    "replaced by" if upload_form.replace_existing.data else "added to from",
                    csv_file.filename, current_bar.id)
            log.info(msg)
            flash(msg, 'success')

        elif 'download' in request.form:
            pass

    return render_template('ingredients.html', form=form, upload_form=upload_form, form_open=form_open)


################################################################################
# API routes
################################################################################
# All of these routes are designed to be used against ajax calls
# Each route will return a json object with the following parameters:
#  status:  "success" - successful go ahead and use the data
#           "error"   - something went wrong
#  message: "..."     - error message
#  data:    {...}     - the expected data returned to caller
def api_error(message, **kwargs):
    return jsonify(status="error", message=message, **kwargs)
def api_success(data, message="", **kwargs):
    return jsonify(status="success", message=message, data=data, **kwargs)

@app.route("/api/ingredients", methods=['GET'])
@login_required
@roles_required('admin')
def api_ingredients():
    ingredients = Ingredient.query.filter_by(bar_id=current_bar.id).order_by(Ingredient.Category, Ingredient.Type).all()
    ingredients = [i.as_dict() for i in ingredients]
    return api_success(ingredients)

@app.route("/api/ingredient", methods=['POST', 'GET', 'PUT', 'DELETE'])
@login_required
@roles_required('admin')
def api_ingredient():
    """CRUD endpoint for individual ingredients

    Indentifying parameters:
    :param string Bottle: bottle for ingredient
    :param string Type: type for ingredient

    Create params:
    :param string Category: Category idenfitier
    :param float ABV: ABV value
    :param float Size: Size
    :param string Unit: one of util.VALID_UNITS
    :param float Price: price of the ingredint

    Read:

    Update:
    :param int row_index: index of the changed row, pass back to requester
    :param string field: the value being modified
    :param string value: the new value (type coerced from field)

    Delete:
    :param int row_index: index of the changed row, pass back to requester

    """
    Bottle = request.form.get('Bottle')
    Type = request.form.get('Type')
    ingredient = Ingredient.query.filter_by(bar_id=current_bar.id, Bottle=Bottle, Type=Type).one_or_none()
    if not ingredient and not request.method == 'POST':
        return api_error("Ingredient not found")
    row_index = request.form.get('row_index')
    if row_index is None and request.method in ['PUT', 'DELETE']:
        return api_error("row_index is a required parameter for {}".fromat(request.method))

    # create
    if request.method == 'POST':
        if ingredient:
            return api_error("Ingredient '{}' already exists, try editing it instead".format(ingredient))
        return api_error("Not implemented")
    # read
    elif request.method == 'GET':
        return api_success(ingredient.as_dict(), messaage="Ingredient: {}".format(ingredient))
    # update
    elif request.method == 'PUT':
        field = request.form.get('field')
        if not field:
            return api_error("'field' is a required parameter")
        elif field not in "Category,Type,Bottle,In_Stock,ABV,Size_mL,Size_oz,Price_Paid".split(','):
            return api_error("'{}' is not allowed to be edited via the API".format(field))
        value = request.form.get('value')
        if not value:
            return api_error("'value' is a required parameter")

        # TODO value constraints
        try:
            # the toggle switches return 'on'/'off'
            # but that is their current state, so toggle value here
            if field == 'In_Stock':
                value = {'on': False, 'off': True}[value]
            else:
                value = type(ingredient[field])(value)
        except AttributeError:
            return api_error("Invalid field '{}' for an Ingredient".format(field))
        except ValueError as e:
            return api_error(str(e))

        # special handling
        if field == 'Size_oz':
            # convert to mL because that's how everything works
            ingredient['Size_mL'] = convert_units(value, 'oz', 'mL')
        else:
            ingredient[field] = value
        if field in ['Size_mL', 'Size_oz', 'Price_Paid', 'Type']:
            _update_computed_fields(ingredient)

        data = ingredient.as_dict()
        db.session.commit()
        mms.regenerate_recipes(current_bar, ingredient=ingredient.type_)
        return api_success(data,
                message=u'Successfully updated "{}" for "{}"'.format(field, ingredient), row_index=row_index)

    # delete
    elif request.method == 'DELETE':
        db.session.delete(ingredient)
        db.session.commit()
        mms.regenerate_recipes(current_bar, ingredient=ingredient.type_)
        return api_success({}, message=u'Successfully deleted "{}"'.format(ingredient), row_index=row_index)

    return api_error("Unknwon method")


@app.route("/admin/debug", methods=['GET'])
@login_required
@roles_required('admin')
def admin_database_debug():
    if app.config.get('DEBUG', False):
        import ipdb; ipdb.set_trace();
        return render_template('result.html', heading="Finished debug session...")
    else:
        return render_template('result.html', heading="Debug unavailable")



################################################################################
# Helper routes
################################################################################

@app.route('/json/<recipe_name>')
def recipe_json(recipe_name):
    recipe_name = urllib.unquote_plus(recipe_name)
    try:
        return jsonify(mms.base_recipes[recipe_name])
    except KeyError:
        return "{} not found".format(recipe_name)


@app.errorhandler(500)
def handle_internal_server_error(e):
    flash(e, 'danger')
    return render_template('result.html', heading="OOPS - Something went wrong..."), 500


@app.route("/api/test")
def api_test():
    a = request.args.get('a', 0, type=int)
    b = request.args.get('b', 0, type=int)
    return jsonify(result=a + b)
