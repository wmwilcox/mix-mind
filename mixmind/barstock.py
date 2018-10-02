import string
import itertools
import logging as log
import copy

try:
    import pandas as pd
    has_pandas = True
except ImportError:
    has_pandas = False

from sqlalchemy import and_, Boolean, DateTime, Column, Integer, ForeignKey, Enum, Float, Unicode
from sqlalchemy.exc import SQLAlchemyError
import csv
import urllib

import util
from .database import db
from .compose_html import close

Categories = 'Spirit Liqueur Vermouth Bitters Syrup Juice Mixer Wine Beer Dry Ice'.split()

# TODO break this out into own module
# TODO value constraints (e.g. 100% max abv, no negative price, etc.)
class Ingredient(db.Model):
    bar_id     = Column(Integer(), ForeignKey('bar.id'), primary_key=True)
    Category   = Column(Enum(*Categories))
    Type       = Column(Unicode(length=100), primary_key=True)
    Bottle     = Column(Unicode(length=255), primary_key=True)
    In_Stock   = Column(Boolean(), default=True)
    ABV        = Column(Float(), default=0.0)
    Size_mL    = Column(Float(), default=0.0)
    Price_Paid = Column(Float(), default=0.0)
    # computed
    type_        = Column(Unicode(length=100))
    Size_oz      = Column(Float(), default=0.0)
    Cost_per_mL  = Column(Float(), default=0.0)
    Cost_per_cL  = Column(Float(), default=0.0)
    Cost_per_oz  = Column(Float(), default=0.0)

    def as_dict(self):
        data = copy.copy(self.__dict__)
        del data['_sa_instance_state']
        return data

    def __str__(self):
        return u"|".join([self.Category, self.Type, self.Bottle])

    def __repr__(self):
        return u"|".join([self.Category, self.Type, self.Bottle])

    def __getitem__(self, field):
        return getattr(self, field)

    def __setitem__(self, field, value):
        return setattr(self, field, value)

    def _uid(self):
        return u"|".join([str(self.bar_id), self.Type, self.Bottle])

    @classmethod
    def query_by_uid(cls, uid):
        bid, t, b = urllib.unquote(uid).split('|')
        return cls.query.filter_by(bar_id=int(bid), Type=t, Bottle=b).one_or_none()

    def instock_toggle(self):
        # make a button to change the stock
        attrs = {'type': "submit", 'target': "_blank", 'name': "toggle-in-stock"}
        if self.In_Stock:
            attrs['class'] = "btn btn-small btn-success"
            attrs['value'] = "&check;"
        else:
            attrs['class'] = "btn btn-small btn-danger"
            attrs['value'] = "&times;"
        uid = close('', 'input', type="hidden", name='uid', value=urllib.quote(self._uid()))
        submit = close('', 'input', **attrs)
        return close('{}{}'.format(uid, submit),
                'form', id='stock-{}'.format(self._uid()), action="", method="post", role="form")

    display_name_mappings = {
        "Category":    {'k':  "Category",     'v':  util.as_utf8},
        "Type":        {'k':  "Type",         'v':  util.as_utf8},
        "Bottle":      {'k':  "Bottle",       'v':  util.as_utf8},
        "In Stock":    {'k':  "In_Stock",     'v':  util.from_bool_from_num},
        "ABV":         {'k':  "ABV",          'v':  util.from_float},
        "Proof":       {'k':  "ABV",          'v':  lambda x: util.from_float(x) / 2.0},
        "Size (mL)":   {'k':  "Size_mL",      'v':  util.from_float},
        "Size (oz)":   {'k':  "Size_oz",      'v':  util.from_float},
        "Price Paid":  {'k':  "Price_Paid",   'v':  util.from_price_float},
        "$/mL":        {'k':  "Cost_per_mL",  'v':  util.from_price_float},
        "$/cL":        {'k':  "Cost_per_cL",  'v':  util.from_price_float},
        "$/oz":        {'k':  "Cost_per_oz",  'v':  util.from_price_float},
    }

def get_barstock_instance(csv_list, use_sql=False, bar_id=None, include_all=False):
    """ Factory for getting the right, initialized barstock
    """
    if isinstance(csv_list, basestring):
        csv_list = [csv_list]
    if use_sql or not has_pandas:
        if bar_id is None:
            raise ValueError("Valid bar object required for sql barstock")
        barstock = Barstock_SQL()
        barstock.load_from_csv(csv_list, bar_id)
        return barstock
    elif has_pandas:
        return Barstock_DF.load(csv_list, include_all=include_all)
    else:
        raise NotImplementedError("No pandas and not using sql version of Barstock")

def _calculated_columns(thing):
    """ Given an object with the required fields,
    calculate and add the other fields
    """
    thing['Size (oz)'] = util.convert_units(thing['Size (mL)'], 'mL', 'oz')
    thing['$/mL'] = thing['Price Paid'] / thing['Size (mL)']
    thing['$/cL'] = thing['Price Paid']*10 / thing['Size (mL)']
    thing['$/oz'] = thing['Price Paid'] / thing['Size (oz)']

def _update_computed_fields(row):
    """ Uses clean names
    """
    row.type_ = row.Type.lower()
    try:
        row.Size_oz = util.convert_units(row.Size_mL, 'mL', 'oz')
        row.Cost_per_mL = row.Price_Paid  / row.Size_mL
        row.Cost_per_cL = row.Price_Paid*10  / row.Size_mL
        row.Cost_per_oz = row.Price_Paid  / row.Size_oz
    except ZeroDivisionError:
        log.warning("Ingredient missing size field: {}".format(row))

class DataError(Exception):
    pass

class Barstock(object):
    pass

class Barstock_SQL(Barstock):
    def __init__(self, bar_id):
        self.bar_id = bar_id
    def load_from_csv(self, csv_list, bar_id, replace_existing=True):
        """Load the given CSVs
        if replace_existing is True, will replace the whole db for this bar
        bar_id is the active bar
        """
        if replace_existing:
            rows_deleted = Ingredient.query.filter_by(bar_id=bar_id).delete()
            log.info("Dropped {} rows for {} table".format(rows_deleted, Ingredient.__tablename__))
        for csv_file in csv_list:
            with open(csv_file) as fp:
                reader = csv.DictReader(fp)
                for row in reader:
                    try:
                        self.add_row(row, bar_id)
                    except DataError as e:
                        log.warning(e)

    def add_row(self, row, bar_id):
        """ where row is a dict of fields from the csv
        returns the Model object for the updated/inserted row"""
        if not row.get('Type') and not row.get('Bottle'):
            log.debug(u"Primary key (Type, Bottle) missing, skipping ingredient: {}".format(row))
            return
        try:
            clean_row = {Ingredient.display_name_mappings[k]['k'] : Ingredient.display_name_mappings[k]['v'](v)
                    for k,v in row.iteritems()
                    if k in Ingredient.display_name_mappings}
        except UnicodeDecodeError:
            log.warning(u"UnicodeDecodeError for ingredient: {}".format(row))
            return None
        try:
            ingredient = Ingredient(bar_id=bar_id, **clean_row)
            row = Ingredient.query.filter_by(bar_id=ingredient.bar_id,
                    Bottle=ingredient.Bottle, Type=ingredient.Type).one_or_none()
            if row: # update
                for k, v in clean_row.iteritems():
                    row[k] = v
                _update_computed_fields(row)
                db.session.commit()
                return row
            else: # insert
                _update_computed_fields(ingredient)
                db.session.add(ingredient)
                db.session.commit()
                return ingredient
        except SQLAlchemyError as err:
            msg = u"{}: on row: {}".format(err, clean_row)
            raise DataError(msg)

    def get_all_bottle_combinations(self, specifiers):
        """ For a given list of ingredient specifiers, return a list of lists
        where each list is a specific way to make the drink
        e.g. Martini passes in ['gin', 'vermouth'], gets [['Beefeater', 'Noilly Prat'], ['Knickerbocker', 'Noilly Prat']]
        """
        bottle_lists = [[b.Bottle for b in self.slice_on_type(i)] for i in specifiers]
        opts = itertools.product(*bottle_lists)
        return opts

    def get_bottle_abv(self, ingredient):
        return self.get_bottle_field(ingredient, u'ABV')

    def get_bottle_category(self, ingredient):
        return self.get_bottle_field(ingredient, u'Category')

    def cost_by_bottle_and_volume(self, ingredient, amount, unit=u'oz'):
        per_unit = self.get_bottle_field(ingredient, u'Cost_per_{}'.format(unit))
        return per_unit * amount

    def get_bottle_field(self, ingredient, field):
        if field not in Ingredient.__table__.columns.keys():
            raise AttributeError(u"get-bottle-field '{}' not a valid field in the data".format(field))
        return self.get_ingredient_row(ingredient)[field]

    def get_ingredient_row(self, ingredient):
        if ingredient.bottle is None:
            raise ValueError(u"ingredient {} has no bottle specified".format(ingredient.__repr__()))
        row = self.slice_on_type(ingredient)
        if len(row) > 1:
            raise ValueError(u'{} has multiple entries in the input data!'.format(ingredient.__repr__()))
        elif len(row) < 1:
            raise ValueError(u'{} has no entry in the input data!'.format(ingredient.__repr__()))
        return row[0]

    # TODO sqlqlchemy exception decorator?
    def slice_on_type(self, specifier):
        """ Return query results for rows matching an ingredient specifier
        Handles several special cases
        """
        type_ = specifier.what.lower()
        if type_ in [u'rum', u'whiskey', u'whisky', u'tequila', u'vermouth']:
            type_ = u'whisk' if type_ == u'whisky' else type_
            filter_ = Ingredient.type_.like(u'%{}%'.format(type_))
        elif type_ == u'any spirit':
            spirits = [u'dry gin', u'rye whiskey', u'bourbon whiskey', u'amber rum', u'dark rum', u'white rum', u'genever', u'cognac', u'brandy', u'aquavit']
            filter_ = Ingredient.type_.in_(spirits)
        elif type_ == u'bitters':
            filter_ = Ingredient.Category == u'Bitters'
        else:
            filter_ = Ingredient.type_ == type_

        if specifier.bottle:
            filter_ = and_(filter_, Ingredient.Bottle == specifier.bottle)

        filter_ = and_(filter_, Ingredient.bar_id == self.bar_id, Ingredient.In_Stock == True)
        return Ingredient.query.filter(filter_).all()

    def to_csv(self):
        cols = Ingredient.__table__.columns.keys()
        result = [u','.join(cols)]
        for row in Ingredient.query.all():
            result.append(u','.join([unicode(row[col]) for col in cols]))
        return u'\n'.join(result)


class Barstock_DF(Barstock):
    """ Wrap up a csv of bottle info with some helpful methods
    for data access and querying
    """

    def __init__(self, df):
        self.df = df

    def get_all_bottle_combinations(self, specifiers):
        """ For a given list of ingredient specifiers, return a list of lists
        where each list is a specific way to make the drink
        e.g. Martini passes in ['gin', 'vermouth'], gets [['Beefeater', 'Noilly Prat'], ['Knickerbocker', 'Noilly Prat']]
        """
        bottle_lists = [self.slice_on_type(i)['Bottle'].tolist() for i in specifiers]
        opts = itertools.product(*bottle_lists)
        return opts

    def get_bottle_abv(self, ingredient):
        return self.get_bottle_field(ingredient, 'ABV')

    def get_bottle_category(self, ingredient):
        return self.get_bottle_field(ingredient, 'Category')

    def cost_by_bottle_and_volume(self, ingredient, amount, unit='oz'):
        per_unit = self.get_bottle_field(ingredient, '$/{}'.format(unit))
        return per_unit * amount

    def get_bottle_field(self, ingredient, field):
        if field not in self.df.columns:
            raise AttributeError("get-bottle-field '{}' not a valid field in the data".format(field))
        return self.get_ingredient_row(ingredient).at[0, field]

    def get_ingredient_row(self, ingredient):
        if ingredient.bottle is None:
            raise ValueError("ingredient {} has no bottle specified".format(ingredient.__repr__()))
        row = self.slice_on_type(ingredient)
        if len(row) > 1:
            raise ValueError('{} has multiple entries in the input data!'.format(ingredient.__repr__()))
        elif len(row) < 1:
            raise ValueError('{} has no entry in the input data!'.format(ingredient.__repr__()))
        return row

    def slice_on_type(self, specifier):
        type_ = specifier.what.lower()
        if type_ in ['rum', 'whiskey', 'whisky', 'tequila', 'vermouth']:
            type_ = 'whisk' if type_ == 'whisky' else type_
            matching = self.df[self.df['type'].str.contains(type_)]
        elif type_ == 'any spirit':
            matching = self.df[self.df.type.isin(['dry gin', 'rye whiskey', 'bourbon whiskey', 'amber rum', 'dark rum', 'white rum', 'genever', 'brandy', 'aquavit'])]
            #matching = self.df[self.df['Category'] == 'Spirit']
        elif type_ == 'bitters':
            matching = self.df[self.df['Category'] == 'Bitters']
        else:
            matching = self.df[self.df['type'] == type_]
        if specifier.bottle:
            return matching[matching['Bottle'] == specifier.bottle].reset_index(drop=True)
        else:
            return matching

    def sorted_df(self):
        return self.df.sort_values(['Category','Type','Price Paid'])


    def add_row(self, row):
        """ where row is a dict """
        _calculated_columns(row)
        row = {k:[v] for k,v in row.iteritems()}
        row = pd.DataFrame.from_dict(row)
        self.df = pd.concat([self.df, row])

    @classmethod
    def load(cls, barstock_csv, include_all=False):
        if isinstance(barstock_csv, basestring):
            barstock_csv = [barstock_csv]
        # TODO validate columns, merge duplicates
        df = pd.concat([pd.read_csv(filename) for filename in barstock_csv])
        df = df.drop_duplicates(['Type', 'Bottle'])
        df = df.dropna(subset=['Type'])
        # convert money columns to floats
        for col in [col for col in df.columns if '$' in col or 'Price' in col]:
            df[col] = df[col].replace('[\$,]', '', regex=True).astype(float)
        df = df.fillna(0)
        _calculated_columns(df)
        df['type'] = map(string.lower, df['Type'])
        df['Category'] = pd.Categorical(df['Category'], Categories)

        # drop out of stock items
        if not include_all:
            #log debug how many dropped
            df = df[df["In Stock"] > 0]
        return cls(df)

