#!/usr/bin/python
# -*- coding: utf-8 -*-
"""Import FEC data.

"""
import csv, sys, cgitb, fixed_width, zipfile, cStringIO, os, glob, time, cPickle
import codecs, re, field_mapper, simplejson, itertools, tempfile, web.utils

def strip(text):
    """
    >>> strip(' s ')
    's'
    """
    return text.strip()

def amount(text):
    """
    Decode amounts according to `FEC_v300.doc` and its kin.

    >>> map(amount, '50.00 6000 6000.00 600000'.split())
    ['50.00', '60.00', '6000.00', '6000.00']
    """
    if '.' in text: return text
    return text[:-2] + '.' + text[-2:]

def name_combo(first, middle, last):
    """
    >>> name_combo('John', '', 'Smith')
    'John Smith'
    >>> name_combo('John', 'Buckminster', 'Smith')
    'John Buckminster Smith'
    >>> name_combo('John', 'B', 'Smith')
    'John B. Smith'

    This is probably incorrectly filed, but comes from a filing on
    2006-04-12.  Should we fix it?
    >>> name_combo('Richard E', '', 'Williams')
    'Richard E Williams'

    This is from a filing from 2005-04-12.
    >>> name_combo(' JOE ', '', 'CAPPETTI ')
    ' JOE  CAPPETTI '

    """
    if len(middle) == 1: middle += '.'
    return ' '.join(filter(None, [first, middle, last]))

def parse_name(name_delim):
    """
    Examples from `FEC_v300.rtf`.
    >>> caret_separated_name = parse_name('^')
    >>> caret_separated_name('Smith^John W.^Dr.^Jr.')
    'Dr. John W. Smith, Jr.'
    >>> caret_separated_name('Smith^John W.^^Jr.')
    'John W. Smith, Jr.'
    >>> caret_separated_name('Smith Medical Corporation')
    'Smith Medical Corporation'
    >>> caret_separated_name('Smith-Reilly^Jennifer T.^Ms.')
    'Ms. Jennifer T. Smith-Reilly'

    Other examples from data:
    >>> caret_separated_name('Field^Tracy C.^^')
    'Tracy C. Field'
    >>> caret_separated_name('Thomson^Linda^Mrs.^')
    'Mrs. Linda Thomson'
    >>> caret_separated_name('Elrod^Adrienne')
    'Adrienne Elrod'

    Should we downcase/titlecase this one?
    >>> caret_separated_name('LOVE^KARA^^')
    'KARA LOVE'

    What were they smoking when they made this one up?
    >>> caret_separated_name('Everett^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^')
    'Everett'

    Some people use a non-caret:
    >>> parse_name('>')('Hagelin>John S')
    'John S Hagelin'

    """
    def rv(name):
        fields = name.split(name_delim)
        while len(fields) < 4: fields.append('')
        while len(fields) > 4:
            assert fields[-1] == ''
            fields.pop()

        lastname, firstname, prefix, suffix = fields
        if prefix: prefix += ' '
        if firstname: firstname += ' '
        if suffix: suffix = ', ' + suffix

        return ''.join([prefix, firstname, lastname, suffix])
    return rv

def schedule_type(data):
    """Is this a contribution, an expenditure, or neither?"""
    if data.get('rec_type') == 'TEXT': return # they have an unhelpful form_type
    if data['form_type'].startswith('SA'): return 'contribution'
    if data['form_type'].startswith('SB'): return 'expenditure'

def date(value):
    if value: return fixed_width.date(value)
    return None                         # to keep Postgres happy

@web.utils.memoize
def mapper_for(name_delim):
    return field_mapper.FieldMapper({
        'date': field_mapper.Reformat(format=date,
                                      source=['date',
                                              'date_received',
                                              'contribution_date',
                                              'expenditure_date',
                                              'date_(of_contribution)',
                                              # XXX this is for SC loans:
                                              'date_(incurred)',
                                              'date_of_expenditure']),
        'candidate_fec_id':
            field_mapper.Reformat(format=strip,
                                  source=['candidate_fec_id',
                                          'candidate_id_number',
                                          'fec_candidate_id_number']
                                  ),
        'tran_id': ['tran_id', 'transaction_id_number'],
        'occupation': ['occupation', 'contributor_occupation', 'indocc'],
        'contributor_org': ['contributor_org',
                            'contributor_organization_name',
                            'contrib_organization_name'],
        'contributor': [field_mapper.Reformat(format=parse_name(name_delim),
                                              source=['contributor_name']),
                        # XXX should include contributor_prefix and
                        # contributor_suffix?
                        lambda contributor_first_name,
                               contributor_middle_name,
                               contributor_last_name:
                            name_combo(contributor_first_name,
                                       contributor_middle_name,
                                       contributor_last_name),
                        ],
        # XXX recipient_name should be reformatted with parse_name(name_delim)
        # at least in 5.00
        # XXX also 'recipient_first_name' 'recipient_last_name'
        # 'recipient_middle_name' 'recipient_organization_name'
        # 'recipient_prefix' 'recipient_suffix'
        'recipient': ['payee_organization_name',
                      'recipient_name',
                      'name_(payee)'],
        'employer': ['employer', 'contributor_employer', 'indemp'],
        'amount': field_mapper.Reformat(format=amount,
                                        source=['amount',
                                                # XXX 6.x contribution_amount:
                                                # different format
                                                'contribution_amount',
                                                'amount_received',
                                                'expenditure_amount', # also 6.x
                                                'amount_of_expenditure']),
        'address': [lambda street__1, street__2, city, state, zip:
                    ' '.join([street__1, street__2, city, state, zip]),
                    lambda contributor_street__1, contributor_street__2,
                           contributor_city, contributor_state, contributor_zip:
                    ' '.join([contributor_street__1,
                              contributor_street__2,
                              contributor_city,
                              contributor_state,
                              contributor_zip])
                    ],

        'committee': ['committee_name',
                      'committee_name_______',
                      'committeename'],
        'candidate': [field_mapper.Reformat(format=parse_name(name_delim),
                                            source='candidate_name'),
                      lambda candidate_first_name,
                             candidate_middle_name,
                             candidate_last_name:
                         name_combo(candidate_first_name,
                                    candidate_middle_name,
                                    candidate_last_name),
                      ],
        'filer_id': ['filer_fec_cand_id',
                     'filer_fec_cmte_id_',
                     'filer_fec_cmte_id',
                     'filer_fec_committee_id',
                     'filer_committee_id_number',
                     'filer_candidate_id_number',
                     "filer's_fec_id_number",
                     'filer_committee_id'],

        'type': field_mapper.CatchAllField(['form_type'], schedule_type),
    })


def _regrtest_fields():
    """
    Regression tests for the `fields` table.

    >>> mapper = mapper_for('^')
    >>> mapped = mapper.map({'date_received': '20081130',
    ...                      'tran_id': '12345', 
    ...                      'weird_field': 34, 
    ...                      'amount_received': '123456'})
    >>> sorted(mapped.keys())
    ['amount', 'date', 'original_data', 'tran_id']
    >>> mapped['date']
    '2008-11-30'
    >>> mapped['amount']
    '1234.56'
    >>> mapped['original_data']['weird_field']
    34
    >>> mapped['tran_id']
    '12345'
    >>> mapper.map({'candidate_id_number': '12345'})
    ... #doctest: +ELLIPSIS
    {'candidate_fec_id': '12345', 'original_data': {...}}
    >>> mapper.map({'fec_candidate_id_number': '56789'})
    ... #doctest: +ELLIPSIS
    {'candidate_fec_id': '56789', 'original_data': {...}}
    >>> mapper.map({'transaction_id_number': '56789'})
    ... #doctest: +ELLIPSIS
    {'original_data': {...}, 'tran_id': '56789'}
    >>> mapper.map({'contributor_occupation': 'Consultant'})
    ... #doctest: +ELLIPSIS
    {'original_data': {...}, 'occupation': 'Consultant'}
    >>> mapper.map({'indocc': 'Private Investor'})
    ... #doctest: +ELLIPSIS
    {'original_data': {...}, 'occupation': 'Private Investor'}
    >>> mapper.map({'indemp': 'EEA Development'})
    ... #doctest: +ELLIPSIS
    {'original_data': {...}, 'employer': 'EEA Development'}

    >>> mapper.map({'street__1': '2531 Falcon Way',
    ...             'street__2': '#400',
    ...             'city': 'Concord',
    ...             'state': 'TX',
    ...             'zip': '20036'})
    ... #doctest: +ELLIPSIS
    {...'address': '2531 Falcon Way #400 Concord TX 20036'...}

    """

class header(csv.excel):
    delimiter = ';'
def headers(filename):
    r = csv.reader(file(filename, 'U'), dialect=header)
    rv = {}
    for line in r:
        # some of the format spec files erroneously say SchA rather than SA
        # or erroneously say SH1, SH2, etc., rather than H1, H2, etc.
        key = line[0].replace('Sch', 'S').replace('SH', 'H')
        rv[key] = [name.strip().lower().replace(' ', '_') for name in line[1:]]
    return rv
def findkey(hmap, key):
    """Find the base schedule or form number,
    given the first field from an FEC filing record.
    """
    while key:
        if key in hmap: return hmap[key]
        else: key = key[:-1]

@web.utils.memoize   # Memoizing saved about 25???40% of run time when I measured
def headers_for_version(version):
    headerdir = os.path.split(__file__)[0]
    return headers(os.path.join(headerdir, 'fec_headers', '%s.csv' % version))

class ascii28separated(csv.excel):
    "The FEC moved from CSV to chr(28)-separated files in format version 6."
    delimiter = chr(28)

class translate_to_utf_8:
    """Convert a presumably Windows-1252 file to UTF-8.

    The FEC???s documents claim non-ASCII characters will be
    rejected, e.g. in FEC_v520.doc:

    > Generally speaking, only keyboard characters are acceptable
    > within CSV files.  Technically, any coded characters that fall
    > outside the range of ASCII characters 32 (space) through 126
    > (tilde ???~???) will be rejected.  Care should be taken if text is
    > cut and pasted from word processing programs, since some
    > characters such as appostrophe [sic] and ???smart quotes??? may not
    > translate into the appropriate ASCII characters.

    However, I have seen a filing (181941.fec, from 20050722.zip,
    version 5.2) in Windows-1252.  Aaron points out:

    > The `chardet` library might be useful:
    > <http://chardet.feedparser.org/>

    Right now we???re not using that, though.

    However, this policy changed by version 6.2, which says:

    > The following characters will be allowed in filing fields (These
    > are technically specified using the ASCII standard):
    > - Keyboard characters. These fall within the range of ASCII 32
    >   (space) through 126 (tilde ???~???).
    > - Some characters used in other languages. Specifically ASCII
    >   characters 128 through 156, ASCII characters 160 through 168,
    >   and ASCII character 173. This allows name and address fields
    >   to contain letters such as ??, ??, ??, ??, ??, ??, etc. Care should
    >   be taken if text is cut and pasted from word processing, or
    >   other programs, since many non-keyboard characters such as
    >   apostrophes and ???smart quotes??? (which are stored as ANSI coded
    >   characters) will not translate into the appropriate ASCII
    >   characters.

    Unfortunately this is nonsense; ASCII is and has always been a
    7-bit code, although there are many ???extended ASCII??? 8-bit
    variants.  The characters they have quoted above exist in the
    commonly-used character set ISO-8859-1, which also does not have
    ???smart quotes???, but ISO-8859-1 have printable characters in the
    range 128 through 156 either.  The most likely character set that
    contains the characters they have quoted and also contains
    printable characters in the 128???156 range is Windows-1252, which
    obsolete parts of Microsoft Windows use by default; however,
    Windows-1252 *does* contain ???smart quotes??? (characters 145???148),
    contains alphabetic characters at codepoints 158 and 159 as well,
    and is missing printable characters at several codepoints inside
    the 128???156 range.

    Due to the absence of any evidence that the FEC is aware that more
    than one character encoding exists, and their acceptance of the
    above-cited filing in Windows-1252 at a time when they officially
    promised not to accept such filings, I am going to assume for the
    time being that all filings are encoded in Windows-1252.

    We???re not using codecs.EncodedFile because it thinks U+001C is a
    line terminator.

    """
    def __init__(self, fileobj):
        self.fileobj = fileobj
        self.encoder = codecs.getencoder('utf-8')
        self.decoder = codecs.getdecoder('windows-1252')
    def readline(self):
        line = self.fileobj.readline()
        # replace errors here for Egl\x8f,Richard,,Mr. and M RUB\x90N
        # HINOJOSA
        unicode_string, length = self.decoder(line, errors='replace')
        assert length == len(line)
        rv, length = self.encoder(unicode_string)
        assert length == len(unicode_string)
        return rv
    def __iter__(self): return iter(self.readline, '')
    def seek(self, position):
        assert position == 0
        self.fileobj.seek(0)

def decode_headerline(line):
    format_version = line[2].strip()

    if format_version < '6':
        headerheaders = 'record_type ef_type fec_ver soft_name soft_ver ' \
                        'name_delim report_id rpt_number'
    else:
        headerheaders = 'record_type ef_type fec_ver soft_name soft_ver ' \
                        'report_id rpt_number'

    headers = dict(zip(headerheaders.split(), (f.strip() for f in line)))
    assert format_version == headers['fec_ver']

    if not headers.get('name_delim'): # empty string means to use the default
        headers['name_delim'] = '^'
    if headers['name_delim'] == '0':     # special case for RNC 2002 software
        headers['name_delim'] = '^'
    assert headers['name_delim'] in '>^' # I want to know if not!

    return headers

class FilingFormatNotDocumented(Exception): pass

# Note that normally we are reading from a zipfile, and Python???s
# stupid zipfile interface doesn???t AFAICT give us the option of
# streaming reads ??? it insists on reading the whole zipfile element at
# once.  So we don???t lose much by parsing from a string rather than a
# file object here, as long as we are careful not to accidentally make
# extra copies of the string.
def readstring(astring):
    # from the Python 2.5 documentation: ???Note: This version of the
    # csv module doesn't support Unicode input. Also, there are
    # currently some issues regarding ASCII NUL
    # characters. Accordingly, all input should be UTF-8 or printable
    # ASCII to be safe; see the examples in section 9.1.5. These
    # restrictions will be removed in the future.???
    fileobj = translate_to_utf_8(cStringIO.StringIO(astring))
    r = csv.reader(fileobj)
    headerline = r.next()
    if chr(28) in headerline[0]:
        # It must be in the new FS-separated format
        fileobj.seek(0)
        r = csv.reader(fileobj, dialect=ascii28separated)
        headerline = r.next()
    if len(headerline) == 1:
        # It???s probably the old 2.x format that we don???t support yet
        # because we can???t find docs; return without yielding
        # anything.
        raise FilingFormatNotDocumented(astring[:20])

    headerdict = decode_headerline(headerline)

    if headerdict.get('rpt_number', '') == '':
        headerdict['rpt_number'] = None

    yield headerdict

    headermap = headers_for_version(headerdict['fec_ver'])
    mapper = mapper_for(headerdict['name_delim'])

    for line in r:
        if not line: continue         # FILPAC inserts random blank lines
        if line[0].lower().strip() in ('[begintext]', '[begin text]'):
            # see e.g. ???New F99 Filing Type for unstructured,
            # formatted text??? in FEC_v300.rtf.  Note that this data
            # may violate `csv`'s expectations, so we have to read it
            # ourselves, and rely on `csv` not doing some kind of
            # read-ahead.
            while True:
                line = fileobj.readline().lower()
                if not line: break      # robustness against premature EOF
                if line.strip() in ('[endtext]', '[end text]',
                                    # NGP Campaign Office(R) 1.0e filing 207928
                                    '[endtext]"'
                                    ):
                    break
                # XXX right now we just discard the lines
            line = r.next()
            # There can be a blank line here too, inserted by e.g. NIC
            # WebForms 6.2.1.1 in filing 353794.fec from 20080722.zip.
            if not line: continue
        fieldnames = findkey(headermap, line[0].upper())
        if not fieldnames:
            raise "could not find field defs", (line[0], headermap.keys())
        rv = mapper.map(dict(zip(fieldnames, line)))
        rv['rpt_number'] = headerdict['rpt_number']
        yield rv

candidate_name_res = [re.compile(x, re.IGNORECASE) for x in
                      [r'''(?ix)(?P<candidate>.*) \s+ for \s+ congress''',
                       r'''(?ix)friends \s+ of \s+ (?P<candidate>.*)''']]
# maybe also:
#  | committee \s+ to \s+ elect (?P<candidate>.*)
# "Alan Pedigo For US House of Rep"
# These are recipients of a certain PAC???s donations:
# "Citizens for Arlen Specter"
# "Ike Skelton for Congress Committee"
# "Bill Nelson for U.S. Senate Campaign"
# "Martin Frost Campaign Committee"
# "Friends of Connie Morella for Congress Committee"
# "Committee to Elect McHugh"

class WrongFirstRecordError(Exception): pass

def read_filing(astring, filename, pathname=None):
    records = readstring(astring)
    # This is where we combine the header_record and the cover_record.
    header_record = records.next()
    cover_record = records.next()
    if not cover_record['original_data']['form_type'].startswith('F'):
        raise WrongFirstRecordError(filename, cover_record)

    cover_record['pathname'] = pathname

    cover_record['this_report_id'] = filename[:-4]
    if header_record.get('report_id'):  # The field may be missing or empty.
        cover_record['report_id'] = \
            re.match('(?i)fec\s*-\s*(\d+)(\.?$|\s+|\*BD[0-9a-f]{4}$)',
                     header_record['report_id']).group(1)
    else:
        cover_record['report_id'] = cover_record['this_report_id']

    cover_record['format_version'] = header_record['fec_ver'] # for debugging
    if not cover_record.get('candidate'):
        for regex in candidate_name_res:
            mo = regex.match(cover_record.get('committee', ''))
            if mo:
                cover_record['candidate'] = mo.group('candidate')
                break
    return cover_record, records

def null_error_handler(): raise

def readfile_zip(filename, handler=null_error_handler):
    zf = zipfile.ZipFile(filename)
    for name in zf.namelist():
        try:
            yield read_filing(zf.read(name), name, pathname=filename)
        except:
            handler()

def readfile_generic(filename, handler=null_error_handler):
    try:
        if filename.endswith('.zip'):
            return readfile_zip(filename, handler)
        else:
            _, basename = os.path.split(filename)
            return [read_filing(file(filename).read(),
                                basename,
                                pathname=filename)]
    except:
        handler()
        return []

EFILINGS_PATH = '../data/crawl/fec/electronic/'
DEFAULT_EFILINGS_FILEPATTERN = EFILINGS_PATH + '*.zip'

def parse_efilings(filenames, handler=null_error_handler):
    last_time = time.time()
    for filename in filenames:
        sys.stderr.write('parsing efilings file %s\n' % filename)
        for parsed_file in readfile_generic(filename, handler):
            yield parsed_file
        now = time.time()
        sys.stderr.write('parsing took %.1f seconds\n' % (now - last_time))
        last_time = now

def atomically_commit_efiling(outfile, tempname, realname):
    outfile.flush()
    os.fsync(outfile.fileno())
    outfile.close()

    os.rename(tempname, realname)

def stash_efilings_worker(destdir, filenames, save_orig):
    cover_record = {}

    def handle_error():
        etype, evalue, etb = sys.exc_info()
        eclass = str if isinstance(etype, basestring) else etype
        if issubclass(eclass, KeyboardInterrupt): raise
        if issubclass(eclass, StopIteration): raise # let it propagate
        if issubclass(eclass, GeneratorExit): raise # let the generator die
        if issubclass(eclass, FilingFormatNotDocumented):
            # We don't bother to log these; we know they happen.
            return

        pathname = cover_record.get('pathname')
        report_id = cover_record.get('this_report_id')

        logdir = os.path.join(destdir, 'errors')
        if not os.path.exists(logdir): os.makedirs(logdir)
        fd, path = tempfile.mkstemp(dir=logdir, suffix = '.' + eclass.__name__)
        fo = os.fdopen(fd, 'w')
        lines_of_context = 5
        fo.write('Surprise; last filing successfully opened was\n%s in %s\n\n' %
                 (report_id, pathname))
        fo.write(cgitb.text((etype, evalue, etb), lines_of_context))
        fo.close()

        sys.stderr.write("logged error (in %s?) to %s, continuing\n" %
                         (report_id, path))

    for efiling in parse_efilings(filenames, handle_error):
        cover_record, records = efiling
        report_id = cover_record['report_id']
        dirpath = os.path.join(destdir, report_id[-2:], report_id)
        if not os.path.exists(dirpath): os.makedirs(dirpath)

        pathname = os.path.join(dirpath,
                                '%s.pck' % cover_record['this_report_id'])

        if os.path.exists(pathname):
            continue

        tempname = pathname + '.new'  # hoping for no concurrent writes
        outfile = file(tempname, 'w')
        if not save_orig: del cover_record['original_data']
        pickle_protocol = 2
        cPickle.dump(cover_record, outfile, pickle_protocol)

        try:
            for record in records:
                if not save_orig: del record['original_data']
                cPickle.dump(record, outfile, pickle_protocol)
        except:
            os.unlink(tempname)
            handle_error()
        else:
            atomically_commit_efiling(outfile, tempname, pathname)

def stash_efilings(destdir=None,
                   filepattern=DEFAULT_EFILINGS_FILEPATTERN,
                   save_orig=False,
                   nprocs=1):
    """Parse a bunch of electronic FEC filings, from zip files or CSV
    files, and store the parsed data in pickle files in a directory
    structure indexed by filing ID.

    * `destdir` is the directory to put the results in.
    * `filepattern` gives the filenames to find the filings in.
    * `save_orig` determines whether to include the full filing data
      in the pickled output, for debugging
    * `nprocs` specifies the number of worker processes to do the
      work.  The filenames are divided up as evenly as possible
      between them.  Because not every file takes the same time to
      process, one process may finish a few files ahead of another,
      but the loss of efficiency to that should be small.

    """
    if destdir is None: destdir = tempfile.mkdtemp()
    filenames = glob.glob(filepattern)
    children = []

    for ii in range(nprocs):
        pid = os.fork()
        if pid == 0:                    # child process
            try:
                myfiles = [filenames[jj] for jj in range(len(filenames))
                           if jj % nprocs == ii]
                stash_efilings_worker(destdir=destdir,
                                      filenames=myfiles,
                                      save_orig=save_orig)
            except:
                try:
                    cgitb.Hook(format='text').handle()
                except:
                    pass
            finally:
                os._exit(0)
        else:
            children.append(pid)

    for pid in children:
        os.waitpid(pid, 0)              # options=0
    
if __name__ == '__main__':
    cgitb.enable(format='text')
    if sys.argv[1] == '--stash-in':
        sys.argv.pop(1)
        destdir = sys.argv.pop(1)
        if len(sys.argv) > 1:
            stash_efilings(destdir=destdir, filepattern=sys.argv[1], nprocs=8)
        else:
            stash_efilings(destdir=destdir, nprocs=8)

    else:
        # pprint is unacceptable --- it made the script run 40?? slower.
        for filename in sys.argv[1:]:
            for form, schedules in readfile_generic(filename):
                print simplejson.dumps(form, sort_keys=True, indent=4)
                for schedule in schedules:
                    print simplejson.dumps(schedule, sort_keys=True, indent=4)
