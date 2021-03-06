import sys
sys.path.insert(0, '/home/halcyon/crypto/anon/opensecrets/FEC/watchdog/vendor/python-clientform')
import web
from zip2rep import zip2dist
from settings import db

import sys
from ClientForm import ControlNotFoundError, AmbiguityError

__all__ = ['ZipShared', 'ZipIncorrect', 'ZipNotFound', 'NoForm', 'WyrError', #all exceptions
            'numdists', 'getdist', 'getcontact', 'getpols', 'has_captcha', 'dist2pols',
            'Form', 'require_captcha']
            
class ZipShared(Exception): pass
class ZipIncorrect(Exception): pass
class ZipNotFound(Exception): pass
class WyrError(Exception): pass
class NoForm(Exception): pass

name_options = dict(prefix=['pre', 'salut', 'title'],
                    lname=['lname', 'last'],
                    fname=['fname', 'first', 'name'],
                    zipcode=['zip', 'zipcode'],
                    zip4=['zip4', 'four', 'plus'],
                    address=['addr1', 'address1', 'add1', 'address', 'add'],
                    addr2=['addr2', 'add2', 'address2'],
                    city=['city'],
                    state=['state'],
                    email=['email', 'e-mail'],
                    phone=['phone'],
                    issue=['issue', 'subject', 'topic', 'title'],
                    subject=['subject', 'topic'],
                    message=['message', 'msg', 'comment', 'text'],
                    captcha=['captcha', 'validat'],
                    reply=['reply', 'response', 'answer']
                )

def numdists(zip5, zip4=None, address=None):
    return len(get_dist(zip5, zip4, address))

def getdist(zip5, zip4=None, address=None):
    query = 'select distinct district_id from zip4 where zip=$zip5'
    if zip4: query += ' and plus4=$zip4'
    query += ' limit 2'     #just to see uniqness of districts
    dist = [x.district_id for x in db.query(query, vars=locals())]
    if len(dist) != 1:
        try:
            dist = zip2dist(zip5, address and address.strip())
        except Exception, details:
            pass
    return dist

def getcontact(pol):
    p = db.select('pol_contacts', where='politician_id=$pol', vars=locals())
    return p[0] if p else {}

def getpols(zip5, zip4=None, address=None):
    dist = getdist(zip5, zip4, address)
    dist = dist[0] if dist else ''
    return dist2pols(dist)

def has_captcha(pol):
    r = db.select('pol_contacts', what='contact', where="politician_id=$pol and captcha='t'", vars=locals())
    return bool(r)

def pol2dist(pol):
    try:
        return db.select('curr_politician', what='district_id', where='curr_politician.id=$pol', vars=locals())[0].district_id
    except KeyError:
        return

def dist2pols(dist):
    if not dist: return []
    where = 'curr_politician.district_id=$dist or curr_politician.district_id=$dist[:2]'
    try:
        return [p.id for p in db.select('curr_politician', what='id', where=where, vars=locals())]
    except KeyError:
        return []

def first(seq):
    """returns first True element"""    
    if not seq: return False
    for s in seq:
        if s: return s

def require_captcha(i, pols=None):
    """returns if the residents of the district defined by zip5, zip4, address in `i`
    have to fill captcha in the pol contact form.
    """
    captcha_fields = [c for c in i if c.startswith('captcha_')]
    captchas_filled = captcha_fields and all([i.get(c) for c in captcha_fields])
    pols = pols or getpols(i.get('zip5'), i.get('zip4'), i.get('addr1', '')+i.get('addr2', ''))
    have_captcha = any(has_captcha(p) for p in pols)
    return (not captchas_filled) and have_captcha

def matches(a, b):
    """`a` matches `b` if any name_options of `a` is a part of `b` in lower case
    >>> matches('name', 'required-fname')
    True
    >>> matches('addr2', 'address2')
    True
    >>> matches('captcha', 'validation')
    True
    >>> matches('lname', 'required-name')
    False
    """
    if not a in name_options.keys():
        return False
    for n in name_options[a]:
        if n in b.lower():
            return True
    return False 

class Form(object):
    def __init__(self, f):
        self.f = f
        self.action = self.f.action
        self.controls = filter(lambda c: not (c.readonly or c.type == 'hidden') and c.name, f.controls)

    def __repr__(self):
        return repr(self.f)

    def __str__(self):
        return str(self.f)

    def  __getattr__(self, x): 
        return getattr(self.f, x)

    def click(self):
        try:
            return self.f.click()
        except Exception, detail:
            print >> sys.stderr, detail

    def fill_name(self, prefix, fname, lname):
        self.fill(prefix, 'prefix')
        if self.fill(lname, 'lname'):
            return self.fill(fname, 'fname')
        else:
            name = "%s %s %s" % (prefix, lname, fname)
            return self.fill(fname, 'fname')

    def fill_address(self, addr1, addr2):    
        if self.fill(addr2, 'addr2'):
            return self.fill(addr1, 'address')
        else:
            address = "%s %s" % (addr1, addr2)
            return self.fill(address, 'address')

    def fill_phone(self, phone):
        phone = phone + ' '* (10 - len(phone)) # make phone length 10
        ph_ctrls = [c.name for c in self.controls if 'phone' in c.name.lower() and c.type == 'text']
        num_ph = len(ph_ctrls)
        if num_ph == 1:
            return self.f.set_value(phone, ph_ctrls[0], type='text', nr=0)
        elif num_ph == 2:
            self.f.set_value(phone[:3], name=ph_ctrls[0], type='text', nr=0)
            self.f.set_value(phone[3:], name=ph_ctrls[1], type='text', nr=0)
        elif num_ph == 3:
            self.f.set_value(phone[:3], name=ph_ctrls[0], type='text', nr=0)
            self.f.set_value(phone[3:6], name=ph_ctrls[1], type='text', nr=0)
            self.f.set_value(phone[6:], name=ph_ctrls[2], type='text', nr=0)

    def select_value(self, control, options):
        if not isinstance(options, list): options = [options]
        items = [str(item).lstrip('*') for item in control.items]
        for option in options:
            for item in items:
                if option.lower() in item.lower():
                    return [item]
        return [item]

    def fill(self, value, name=None, type=None, control=None):
        c = control or self.find_control(name=name, type=type)
        if c:
            if c.type in ['select', 'radio', 'checkbox']: 
                value = self.select_value(c, value)
            elif isinstance(value, list):
                value = value[0]
            self.f.set_value(value, name=c.name, type=c.type, nr=0)
            return True 
        return False

    def fill_all(self, **d):
        #fill all the fields of the form with the values from `d`
        for c in self.controls:
            filled = False
            if c.name in d.keys():
                filled = self.fill(d[c.name], control=c)
            else:
                for k in d.keys():
                    if matches(k, c.name):
                        filled = self.fill(d[k], control=c)
            #if not filled and not c.value: print "couldn't fill %s" % (c.name),

    def has(self, name=None, type=None):
        return bool(self.find_control(name=name, type=type))

    def find_control(self, name=None, type=None):
        """return the form control of type `type` or matching `name_options` of `name`"""
        if not (name or type): return

        try:
            names = name_options[name]
        except KeyError:
            names = name and [name]
        c = None
        if type: c = self.find_control_by_type(type)
        if not c and names: c = first(self.find_control_by_name(name) for name in names)
        if not c and names: c = first(self.find_control_by_id(name) for name in names)

        return c     

    def find_control_by_name(self, name):
        name = name.lower()
        return first(c for c in self.controls if c.name and name in c.name.lower())

    def find_control_by_id(self, id):
        id = id.lower()
        return first(c for c in self.controls if c.id and id in c.id.lower())

    def find_control_by_type(self, type):
        try:
            return self.f.find_control(type=type)
        except ControlNotFoundError:
            return None
        except AmbiguityError:  #@@  TO BE FIXED
            return self.f.find_control(type=type, nr=1)

