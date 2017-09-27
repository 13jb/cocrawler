'''
URL class and transformations for the cocrawler.

We apply "safe" transformations early and often.

We apply "unsafe" transformations right after parsing an url out of
a webpage. (These represent things that browsers do but aren't
in the RFC, like discarding /r/n in the middle of hostnames.)

See cocrawler/data/html-parsin-test.html for an analysis of browser
transformations.




'''

from collections import namedtuple
import urllib.parse
import logging
import re

import tldextract

from . import surt

LOGGER = logging.getLogger(__name__)


'''
Notes from reading RFC 3986:

General rule: always unquote A-Za-z0-9-._~  # these are never delims
  called 'unreserved' in the rfc ... x41-x5a x61-x7a x30-x39 x2d x2e x5f x7e

reserved:
 general delims :/?#[]@
 sub delims !$&'()*+,;=

scheme blah blah
netloc starts with //, ends with /?# and has internal delims of :@
    hostname can be ip4 literal or [ip4 or ip6 literal] so also dots (ipv4) and : (ipv6)
      (this is the only place where [] are allowed unquoted)
path
    a character in a path is unreserved %enc sub-delims :@ and / is the actual delimiter
      so, general-delims other than :/@ must be quoted & kept that way
      that means ?#[] need quoting
    . and .. are special (see section 5.2)
    sub-delims can be present and don't have to be quoted
query
    same as path chars but adds /? to chars allowed
     so #[] still need quoting
    we are going to split query up using &= which are allowed characters
fragment
    same chars as query

due to quoting, % must be quoted

'''


def is_absolute_url(url):
    if url[0:2] == '//':
        return True
    # TODO: allow more schemes
    if url[0:7].lower() == 'http://' or url[0:8].lower() == 'https://':
        return True
    return False


def clean_webpage_links(link, urljoin=None):
    '''
    Webpage links have lots of random crap in them, which browsers tolerate,
    that we'd like to clean up before calling urljoin() on them.

    Also, since cocrawler allows a variety of html parsers, it's likely that
    we will get improperly-terminated urls that result in the parser returning
    the rest of the webpage as an url, etc etc.

    Some of these come from
    https://github.com/django/django/blob/master/django/utils/http.py#L287
    and https://bugs.chromium.org/p/chromium/issues/detail?id=476478

    See manual tests in cocrawler/data/html-parsing-test.html

    TODO: headless browser testing to automate this
    '''

    # remove leading and trailing white space and unescaped control chars.
    link = link.strip('\x00\x01\x02\x03\x04\x05\x06\x07\x08\x09\x0a\x0b\x0c\x0d\x0e\x0f'
                      '\x10\x11\x12\x13\x14\x15\x16\x17\x18\x19\x1a\x1b\x1c\x1d\x1e\x1f ')

    # FF and Chrome interpret both ///example.com and http:///example.com as a hostname
    m = re.match(r'(?:https?:)?/{3,}', link, re.I)
    if m:
        start = m.group(0)
        link = start.rstrip('/') + '//' + link.replace(start, '', 1)
    # ditto for \\\ -- and go ahead and fix up http:\\ while we're here
    m = re.match(r'(?:https?:)?\\{2,}', link, re.I)
    if m:
        start = m.group(0)
        link = start.rstrip('\\') + '//' + link.replace(start, '', 1)

    # and the \ that might be after the hostname?
    if is_absolute_url(link):
        start = link.find('://') + 3  # works whether we have a scheme or not
        m = re.search(r'[\\/?#]', link[start:])
        if m:
            if m.group(0) == '\\':
                link = link[0:start] + link[start:].replace('\\', '/', 1)

    '''
    Runaway urls

    We allow pluggable parsers, and some of them might non-clever and send us the entire
    rest of the document as an url... or it could be that the webpage lacks a closing
    quote for one of its urls, which can confuse diligent parsers.

    There are formal rules for this in html5, by testing I see that FF and Chrome both
    truncate *undelimited* urls at the first \>\r\n

    We have no idea which urls were delimited or not at this point. So, only molest
    ones which seem awfully long.
    '''

    if len(link) > 300:  # arbitrary choice
        m = re.match(r'(.*?)[<>\"\'\r\n ]', link)  # rare  in urls and common in html markup
        if m:
            link = m.group(1)
        if len(link) > 300:
            LOGGER.info('webpage urljoin=%s has an invalid-looking link %s', str(urljoin), link)
            return ''  # will urljoin to the urljoin

    # remove unquoted \r and \n anywhere in the url
    link = link.replace('\r', '')
    link = link.replace('\n', '')

    return link


def special_seed_handling(url):
    '''
    We don't expect seed-lists to be very clean: no scheme, etc.
    '''
    # use urlsplit to accurately test if a scheme is present
    parts = urllib.parse.urlsplit(url)
    if parts.scheme == '':
        if url.startswith('//'):
            url = 'http:' + url
        else:
            url = 'http://' + url
    return url


def remove_dot_segments(path):
    '''
    Algorithm from RFC 3986. urllib.parse has this algorithm, but it's hidden in urljoin()
    This is a stand-alone version. Since this is working on a non-relative url, path MUST begin with '/'
    '''
    if path[0] != '/':
        raise ValueError('Invalid path, must start with /: '+path)

    segments = path.split('/')
    # drop empty segment pieces to avoid // in output... but not the first segment
    segments[1:-1] = filter(None, segments[1:-1])
    resolved_segments = []
    for s in segments[1:]:
        if s == '..':
            try:
                resolved_segments.pop()
            except IndexError:
                # discard the .. if it's at the beginning
                pass
        elif s == '.':
            continue
        else:
            resolved_segments.append(s)
    return '/' + '/'.join(resolved_segments)


valid_hex = set('%02x' % i for i in range(256))
valid_hex.update(set('%02X' % i for i in range(256)))

unreserved = set('%02X' % i for i in range(0x41, 0x5b))  # A-Z
unreserved.update(set('%02X' % i for i in range(0x61, 0x7b)))  # a-z
unreserved.update(set('%02X' % i for i in range(0x30, 0x3a)))  # 0-9
unreserved.update(set(('2D', '2E', '5F', '7E')))  # -._~

subdelims = set(('21', '24', '3B', '3D'))  # !$;=
subdelims.update(set('%02X' % i for i in range(0x26, 0x2d)))  # &'()*+,

unquote_in_path = subdelims.copy()
unquote_in_path.update(set(('3A', '40')))  # ok: :@

unquote_in_query = subdelims.copy()
unquote_in_query.update(set(('3A', '2F', '3F', '40')))  # ok: :/?@
unquote_in_query.remove('26')  # not ok: &=
unquote_in_query.remove('3D')

unquote_in_frag = unquote_in_query.copy()


def unquote(text, safe):
    pieces = text.split('%')
    text = pieces.pop(0)
    for p in pieces:
        if text.endswith('%'):  # deal with %%
            text += '%' + p
            continue
        quote = p[:2]
        rest = p[2:]
        if quote in valid_hex:
            quote = quote.upper()
        if quote in safe:
            text += chr(int(quote, base=16)) + rest
        else:
            text += '%' + quote + rest
    return text


def safe_url_canonicalization(url):
    '''
    Do everything to the url which should not change it
    Good discussion: https://en.wikipedia.org/wiki/URL_normalization
    '''

    url = unquote(url, unreserved)

    try:
        (scheme, netloc, path, query, fragment) = urllib.parse.urlsplit(url)
    except ValueError:
        LOGGER.info('invalid url %s', url)
        raise

    scheme = scheme.lower()

    netloc = surt.netloc_to_punycanon(scheme, netloc)

    if path == '':
        path = '/'
    path = remove_dot_segments(path)
    path = path.replace('\\', '/')  # might not be 100% safe but is needed for Windows buffoons
    path = unquote(path, unquote_in_path)

    query = unquote(query, unquote_in_query)

    if fragment is not '':
        fragment = '#' + unquote(fragment, unquote_in_frag)

    return urllib.parse.urlunsplit((scheme, netloc, path, query, None)), fragment


def upgrade_url_to_https(url):
    # TODO
    #  use browser HSTS list to upgrade to https:
    #   https://chromium.googlesource.com/chromium/src/net/+/master/http/transport_security_state_static.json
    #  use HTTPSEverwhere? would have to have a fallback if https failed / redir to http
    return


def special_redirect(url, next_url):
    '''
    Classifies some redirects that we wish to do special processing for

    XXX the case where SURT(url) == SURT(redirect) needs to be handled: 'samesurt'
    '''

    if abs(len(url.url) - len(next_url.url)) > 5:  # 5 = 'www.' + 's'  # what about 'samesurt' ?
        return None

    if url.url == next_url.url:
        return 'same'

    if not url.url.endswith('/') and url.url + '/' == next_url.url:
        return 'addslash'

    if url.url.endswith('/') and url.url == next_url.url + '/':
        return 'removeslash'

    if url.url.replace('http', 'https', 1) == next_url.url:
        return 'tohttps'
    if url.url.startswith('https') and url.url.replace('https', 'http', 1) == next_url.url:
        return 'tohttp'

    if url.urlsplit.netloc.startswith('www.'):
        if url.url.replace('www.', '', 1) == next_url.url:
            return 'tononwww'
        else:
            if url.url.replace('www.', '', 1).replace('http', 'https', 1) == next_url.url:
                return 'tononwww+tohttps'
            elif (url.url.startswith('https') and
                  url.url.replace('www.', '', 1).replace('https', 'http', 1) == next_url.url):
                return 'tononwww+tohttp'
    elif next_url.urlsplit.netloc.startswith('www.'):
        if url.url == next_url.url.replace('www.', '', 1):
            return 'towww'
        else:
            if next_url.url.replace('www.', '', 1) == url.url.replace('http', 'https', 1):
                return 'towww+tohttps'
            elif (url.url.startswith('https') and
                  next_url.url.replace('www.', '', 1) == url.url.replace('https', 'http', 1)):
                return 'towww+tohttp'

    return None


def get_domain(hostname):
    # TODO config option to set include_psl_private_domains=True ?
    #  sometimes we do want *.blogspot.com to all be different tlds
    #  right now set externally, see https://github.com/john-kurkowski/tldextract/issues/66
    #  the makefile for this repo sets it to private and there is a unit test for it
    return tldextract.extract(hostname).registered_domain


def get_hostname(url, parts=None, remove_www=False):
    # TODO: also duplicated in url_allowed.py
    # XXX audit code for other places www is explicitly mentioned
    if not parts:
        parts = urllib.parse.urlsplit(url)
    hostname = parts.netloc
    if remove_www and hostname.startswith('www.'):
        domain = get_domain(hostname)
        if not domain.startswith('www.'):
            hostname = hostname[4:]
    return hostname


# stolen from urllib/parse.py
SplitResult = namedtuple('SplitResult', 'scheme netloc path query fragment')


class URL(object):
    '''
    Container for urls and url processing.
    Precomputes a lot of stuff upon creation, which is usually done in a burner thread.
    Currently idempotent.
    '''
    def __init__(self, url, urljoin=None, seed=False):
        if seed:
            url = special_seed_handling(url)
        url = clean_webpage_links(url, urljoin=urljoin)

        if urljoin:
            if isinstance(urljoin, str):
                urljoin = URL(urljoin)
            # optimize a few common cases to dodge full urljoin cost
            if url.startswith('http://') or url.startswith('https://'):
                pass
            elif url.startswith('/') and not url.startswith('//'):
                url = urljoin.urlsplit.scheme + '://' + urljoin.hostname + url
            else:
                url = urllib.parse.urljoin(urljoin.url, url)  # expensive

        # TODO safe_url_canon has the parsed url, have it pass back the parts
        url, frag = safe_url_canonicalization(url)

        if len(frag) > 0:
            self._original_frag = frag
        else:
            self._original_frag = None

        try:
            self._urlsplit = urllib.parse.urlsplit(url)  # expensive
        except ValueError:
            LOGGER.info('invalid url %s sent into URL constructor', url)
            # TODO: my code assumes URL() returns something valid, so...
            raise

        (scheme, netloc, path, query, _) = self._urlsplit

        if path == '':
            path = '/'

        # TODO: there's a fair bit of duplicate computing in here
        netloc = surt.netloc_to_punycanon(scheme, netloc)
        self._netloc = netloc
        self._hostname = surt.hostname_to_punycanon(netloc)
        self._hostname_without_www = surt.discard_www_from_hostname(self._hostname)
        self._surt = surt.surt(url)

        self._urlsplit = SplitResult(scheme, netloc, path, query, '')
        self._url = urllib.parse.urlunsplit(self._urlsplit)  # final canonicalization
        self._registered_domain = tldextract.extract(self._url).registered_domain

    @property
    def url(self):
        return self._url

    def __str__(self):
        return self._url

    @property
    def urlsplit(self):
        return self._urlsplit

    @property
    def netloc(self):
        return self._netloc

    @property
    def hostname(self):
        return self._hostname

    @property
    def hostname_without_www(self):
        return self._hostname_without_www

    @property
    def surt(self):
        return self._surt

    @property
    def registered_domain(self):
        return self._registered_domain

    @property
    def original_frag(self):
        return self._original_frag
