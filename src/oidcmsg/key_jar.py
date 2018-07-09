import json
import logging
import os

from cryptojwt import as_bytes
from cryptojwt import as_unicode
from cryptojwt import b64e
from cryptojwt import jwe
from cryptojwt import jws
from cryptojwt.jwk import DeSerializationNotPossible

from oidcmsg.exception import MessageException
from oidcmsg.exception import OidcMsgError
from oidcmsg.key_bundle import KeyBundle
from oidcmsg.key_bundle import ec_init
from oidcmsg.key_bundle import rsa_init

__author__ = 'Roland Hedberg'

KEYLOADERR = "Failed to load %s key from '%s' (%s)"
REMOTE_FAILED = "Remote key update from '{}' failed, HTTP status {}"
MALFORMED = "Remote key update from {} failed, malformed JWKS."

logger = logging.getLogger(__name__)


def raise_exception(excep, descr, error='service_error'):
    _err = json.dumps({'error': error, 'error_description': descr})
    raise excep(_err, 'application/json')


class KeyIOError(OidcMsgError):
    pass


class UnknownKeyType(KeyIOError):
    pass


class UpdateFailed(KeyIOError):
    pass


class KeyJar(object):
    """ A keyjar contains a number of KeyBundles """

    def __init__(self, ca_certs=None, verify_ssl=True, keybundle_cls=KeyBundle,
                 remove_after=3600):
        """
        KeyJar init function
        
        :param ca_certs: CA certificates, to be used for HTTPS
        :param verify_ssl: Attempting SSL certificate verification
        :return: Keyjar instance
        """
        self.spec2key = {}
        self.issuer_keys = {}
        self.ca_certs = ca_certs
        self.verify_ssl = verify_ssl
        self.keybundle_cls = keybundle_cls
        self.remove_after = remove_after

    def __repr__(self):
        issuers = list(self.issuer_keys.keys())
        return '<KeyJar(issuers={})>'.format(issuers)

    def add_url(self, owner, url, **kwargs):
        """
        Add a set of keys by url. This method will create a 
        :py:class:`oidcmsg.key_bundle.KeyBundle` instance with the
        url as source specification. If no fileformat is given it's assumed
        that what's on the other side is a JWKS.
        
        :param owner: Who issued the keys
        :param url: Where can the key/-s be found
        :param kwargs: extra parameters for instantiating KeyBundle
        :return: A :py:class:`oidcmsg.oauth2.keybundle.KeyBundle` instance
        """

        if not url:
            raise KeyError("No jwks_uri")

        if "/localhost:" in url or "/localhost/" in url:
            kc = self.keybundle_cls(source=url, verify_ssl=False, **kwargs)
        else:
            kc = self.keybundle_cls(source=url, verify_ssl=self.verify_ssl,
                                    **kwargs)

        try:
            self.issuer_keys[owner].append(kc)
        except KeyError:
            self.issuer_keys[owner] = [kc]

        return kc

    def add_symmetric(self, owner, key, usage=None):
        """
        Add a symmetric key. This is done by wrapping it in a key bundle 
        cloak since KeyJar does not handle keys directly but only through
        key bundles.
        
        :param owner: Owner of the key
        :param key: The key 
        :param usage: What the key can be used for signing/signature 
            verification (sig) and/or encryption/decryption (enc)
        """
        if owner not in self.issuer_keys:
            self.issuer_keys[owner] = []

        _key = b64e(as_bytes(key))
        if usage is None:
            self.issuer_keys[owner].append(
                self.keybundle_cls([{"kty": "oct", "k": _key}]))
        else:
            for use in usage:
                self.issuer_keys[owner].append(
                    self.keybundle_cls([{"kty": "oct",
                                         "k": _key,
                                         "use": use}]))

    def add_kb(self, owner, kb):
        """
        Add a key bundle and bind it to an identifier
        
        :param owner: Owner of the keys in the keybundle
        :param kb: A :py:class:`oidcmsg.key_bundle.KeyBundle` instance
        """
        try:
            self.issuer_keys[owner].append(kb)
        except KeyError:
            self.issuer_keys[owner] = [kb]

    def __setitem__(self, owner, val):
        """
        Bind one or a list of key bundles to a special identifier.
        Will overwrite whatever was there before !!
        
        :param owner: The owner of the keys in the keybundle/-s
        :param val: A single or a list of KeyBundle instance
        :return: 
        """
        if not isinstance(val, list):
            val = [val]

        for kb in val:
            if not isinstance(kb, KeyBundle):
                raise ValueError('{} not an KeyBundle instance'.format(kb))

        self.issuer_keys[owner] = val

    def items(self):
        """
        Get all owner ID's and there key bundles
        
        :return: list of 2-tuples (Owner ID., list of KeyBundles)
        """
        return self.issuer_keys.items()

    def get(self, key_use, key_type="", owner="", kid=None, **kwargs):
        """
        Get all keys that matches a set of search criteria

        :param key_use: A key useful for this usage (enc, dec, sig, ver)
        :param key_type: Type of key (rsa, ec, oct, ..)
        :param owner: Who is the owner of the keys, "" == me
        :param kid: A Key Identifier
        :return: A possibly empty list of keys
        """

        if key_use in ["dec", "enc"]:
            use = "enc"
        else:
            use = "sig"

        _kj = None
        if owner != "":
            try:
                _kj = self.issuer_keys[owner]
            except KeyError:
                if owner.endswith("/"):
                    try:
                        _kj = self.issuer_keys[owner[:-1]]
                    except KeyError:
                        pass
                else:
                    try:
                        _kj = self.issuer_keys[owner + "/"]
                    except KeyError:
                        pass
        else:
            try:
                _kj = self.issuer_keys[owner]
            except KeyError:
                pass

        if _kj is None:
            return []

        lst = []
        for bundle in _kj:
            if key_type:
                _bkeys = bundle.get(key_type)
            else:
                _bkeys = bundle.keys()
            for key in _bkeys:
                if key.inactive_since and key_use != "sig":
                    # Skip inactive keys unless for signature verification
                    continue
                if not key.use or use == key.use:
                    if kid:
                        if key.kid == kid:
                            lst.append(key)
                            break
                        else:
                            continue
                    else:
                        lst.append(key)

        # if elliptic curve have to check I have a key of the right curve
        if key_type == "EC" and "alg" in kwargs:
            name = "P-{}".format(kwargs["alg"][2:])  # the type
            _lst = []
            for key in lst:
                if name != key.crv:
                    continue
                _lst.append(key)
            lst = _lst

        if use == 'enc' and key_type == 'oct' and owner != '':
            # Add my symmetric keys
            for kb in self.issuer_keys['']:
                for key in kb.get(key_type):
                    if key.inactive_since:
                        continue
                    if not key.use or key.use == use:
                        lst.append(key)

        return lst

    def get_signing_key(self, key_type="", owner="", kid=None, **kwargs):
        return self.get("sig", key_type, owner, kid, **kwargs)

    def get_verify_key(self, key_type="", owner="", kid=None, **kwargs):
        return self.get("ver", key_type, owner, kid, **kwargs)

    def get_encrypt_key(self, key_type="", owner="", kid=None, **kwargs):
        return self.get("enc", key_type, owner, kid, **kwargs)

    def get_decrypt_key(self, key_type="", owner="", kid=None, **kwargs):
        return self.get("dec", key_type, owner, kid, **kwargs)

    def keys_by_alg_and_usage(self, issuer, alg, usage):
        if usage in ["sig", "ver"]:
            ktype = jws.alg2keytype(alg)
        else:
            ktype = jwe.alg2keytype(alg)

        return self.get(usage, ktype, issuer)

    def get_issuer_keys(self, issuer):
        res = []
        for kbl in self.issuer_keys[issuer]:
            res.extend(kbl.keys())
        return res

    def __contains__(self, item):
        if item in self.issuer_keys:
            return True
        else:
            return False

    def __getitem__(self, owner):
        try:
            return self.issuer_keys[owner]
        except KeyError:
            logger.debug(
                "Owner '{}' not found, available key owners: {}".format(
                    owner, list(self.issuer_keys.keys())))
            raise

    def owners(self):
        return self.issuer_keys.keys()

    def match_owner(self, url):
        for owner in self.issuer_keys.keys():
            if url.startswith(owner):
                return owner

        raise KeyIOError("No keys for '%s'" % url)

    def __str__(self):
        _res = {}
        for _id, kbs in self.issuer_keys.items():
            _l = []
            for kb in kbs:
                _l.extend(json.loads(kb.jwks())["keys"])
            _res[_id] = {"keys": _l}
        return "%s" % (_res,)

    def load_keys(self, pcr, issuer, replace=False):
        """
        Fetch keys from another server

        :param pcr: The provider information
        :param issuer: The provider URL
        :param replace: If all previously gathered keys from this provider
            should be replace.
        :return: Dictionary with usage as key and keys as values
        """

        logger.debug("Initiating key bundle for issuer: %s" % issuer)
        try:
            logger.debug("pcr: %s" % pcr)
        except MessageException:
            pass

        if replace or issuer not in self.issuer_keys:
            self.issuer_keys[issuer] = []

        try:
            self.add_url(issuer, pcr["jwks_uri"])
        except KeyError:
            # jwks should only be considered if no jwks_uri is present
            try:
                _keys = pcr["jwks"]["keys"]
                self.issuer_keys[issuer].append(
                    self.keybundle_cls(_keys, verify_ssl=self.verify_ssl))
            except KeyError:
                pass

    def find(self, source, issuer):
        """
        Find a key bundle based on the source of the keys

        :param source: A source url
        :param issuer: The issuer of keys
        """
        try:
            for kb in self.issuer_keys[issuer]:
                if kb.source == source:
                    return kb
        except KeyError:
            return None

    def export_jwks(self, private=False, issuer=""):
        """
        Produces a dictionary that later can be easily mapped into a 
        JSON string representing a JWKS.
        
        :param private: 
        :param issuer: 
        :return: 
        """
        keys = []
        for kb in self.issuer_keys[issuer]:
            keys.extend([k.serialize(private) for k in kb.keys() if
                         k.inactive_since == 0])
        return {"keys": keys}

    def export_jwks_as_json(self, private=False, issuer=""):
        return json.dumps(self.export_jwks(private, issuer))

    def import_jwks(self, jwks, issuer):
        """

        :param jwks: Dictionary representation of a JWKS
        :param issuer: Who 'owns' the JWKS
        """
        try:
            _keys = jwks["keys"]
        except KeyError:
            raise ValueError('Not a proper JWKS')
        else:
            try:
                self.issuer_keys[issuer].append(
                    self.keybundle_cls(_keys, verify_ssl=self.verify_ssl))
            except KeyError:
                self.issuer_keys[issuer] = [self.keybundle_cls(
                    _keys, verify_ssl=self.verify_ssl)]

    def import_jwks_as_json(self, js, issuer):
        return self.import_jwks(json.loads(js), issuer)

    def __eq__(self, other):
        if not isinstance(other, KeyJar):
            return False

        # The set of issuers MUST be the same
        if set(self.owners()) != set(other.owners()):
            return False

        # Keys per issuer must be the same
        for iss in self.owners():
            sk = self.get_issuer_keys(iss)
            ok = other.get_issuer_keys(iss)
            if len(sk) != len(ok):
                return False

            if not any(k in ok for k in sk):
                return False

        return True

    def remove_outdated(self, when=0):
        """
        Goes through the complete list of issuers and for each of them removes
        outdated keys.
        Outdated keys are keys that has been marked as inactive at a time that
        is longer ago then some set number of seconds.
        The number of seconds a carried in the remove_after parameter.

        :param when: To facilitate testing
        """
        for iss in list(self.owners()):
            _kbl = []
            for kb in self.issuer_keys[iss]:
                kb.remove_outdated(self.remove_after, when=when)
                if len(kb):
                    _kbl.append(kb)
            if _kbl:
                self.issuer_keys[iss] = _kbl
            else:
                del self.issuer_keys[iss]

    def _add_key(self, keys, owner, use, key_type='', kid='',
                 no_kid_issuer=None, allow_missing_kid=False):

        if owner not in self:
            logger.error('Issuer "{}" not in keyjar'.format(owner))
            return keys

        logger.debug('Key set summary for {}: {}'.format(
            owner, key_summary(self, owner)))

        if kid:
            for _key in self.get(key_use=use, owner=owner, kid=kid,
                                 key_type=key_type):
                if _key and _key not in keys:
                    keys.append(_key)
            return keys
        else:
            try:
                kl = self.get(key_use=use, owner=owner, key_type=key_type)
            except KeyError:
                pass
            else:
                if len(kl) == 0:
                    return keys
                elif len(kl) == 1:
                    if kl[0] not in keys:
                        keys.append(kl[0])
                elif allow_missing_kid:
                    keys.extend(kl)
                elif no_kid_issuer:
                    try:
                        allowed_kids = no_kid_issuer[owner]
                    except KeyError:
                        return keys
                    else:
                        if allowed_kids:
                            keys.extend(
                                [k for k in kl if k.kid in allowed_kids])
                        else:
                            keys.extend(kl)
        return keys

    def get_jwt_decrypt_keys(self, jwt, **kwargs):
        """
        Get decryption keys from a keyjar. 
        These keys should be usable to decrypt an encrypted JWT.

        :param jwt: A cryptojwt.jwt.JWT instance
        :param kwargs: Other key word arguments
        :return: list of usable keys
        """


        try:
            _key_type = jwe.alg2keytype(jwt.headers['alg'])
        except KeyError:
            _key_type = ''

        try:
            _kid = jwt.headers['kid']
        except KeyError:
            logger.info('Missing kid')
            _kid = ''

        keys = self.get(key_use='enc', owner='', key_type=_key_type)
        keys = self._add_key(keys, '', 'enc', _key_type, _kid, {'': None})

        # Only want the private keys. Symmetric keys are also fine
        keys = [k for k in keys if k.is_private_key()]

        return keys

    def get_jwt_verify_keys(self, jwt, **kwargs):
        """
        Get keys from a keyjar. These keys should be usable to verify a 
        signed JWT.

        :param jwt: A cryptojwt.jwt.JWT instance
        :param kwargs: Other key word arguments
        :return: list of usable keys
        """

        try:
            allow_missing_kid = kwargs['allow_missing_kid']
        except KeyError:
            allow_missing_kid = False

        try:
            _key_type = jws.alg2keytype(jwt.headers['alg'])
        except KeyError:
            _key_type = ''

        try:
            _kid = jwt.headers['kid']
        except KeyError:
            logger.info('Missing kid')
            _kid = ''

        try:
            nki = kwargs['no_kid_issuer']
        except KeyError:
            nki = {}

        keys = self.get(key_use='sig', owner='', key_type=_key_type)

        _payload = jwt.payload()

        try:
            _iss = _payload['iss']
        except KeyError:
            try:
                _iss = kwargs['iss']
            except KeyError:
                _iss = ''

        if _iss:
            keys = self._add_key(keys, _iss, 'sig', _key_type,
                                 _kid, nki, allow_missing_kid)

        # First extend the keyjar if allowed
        if "jku" in jwt.headers and _iss:
            if not self.find(jwt.headers["jku"], _iss):
                # This is really questionable
                try:
                    if kwargs["trusting"]:
                        self.add_url(_iss, jwt.headers["jku"])
                except KeyError:
                    pass

        for ent in ["aud", "client_id"]:
            if ent not in _payload:
                continue
            if ent == "aud":
                # list or basestring
                if isinstance(_payload["aud"], str):
                    _aud = [_payload["aud"]]
                else:
                    _aud = _payload["aud"]
                for _e in _aud:
                    keys = self._add_key(keys, _e, 'sig', _key_type, _kid,
                                         nki, allow_missing_kid)
            else:
                keys = self._add_key(keys, _payload[ent], 'sig', _key_type,
                                     _kid, nki, allow_missing_kid)

        # Only want the public keys. Symmetric keys are also OK.
        keys = [k for k in keys if k.is_public_key()]
        return keys

    def copy(self):
        kj = KeyJar()
        for owner in self.owners():
            kj[owner] = [kb.copy() for kb in self[owner]]
        return kj


# =============================================================================


def build_keyjar(key_conf, kid_template="", keyjar=None):
    """
    Configuration of the type ::
    
        keys = [
            {"type": "RSA", "key": "cp_keys/key.pem", "use": ["enc", "sig"]},
            {"type": "EC", "crv": "P-256", "use": ["sig"]},
            {"type": "EC", "crv": "P-256", "use": ["enc"]}
        ]
    
    
    :param key_conf: The key configuration
    :param kid_template: A template by which to build the kids
    :param keyjar: If an KeyJar instance the new keys are added to this key jar.
    :return: A KeyJar instance
    """

    if keyjar is None:
        keyjar = KeyJar()

    kid = 0

    for spec in key_conf:
        typ = spec["type"].upper()

        kb = {}
        if typ == "RSA":
            if "key" in spec:
                error_to_catch = (OSError, IOError,
                                  DeSerializationNotPossible)
                try:
                    kb = KeyBundle(source="file://%s" % spec["key"],
                                   fileformat="der",
                                   keytype=typ, keyusage=spec["use"])
                except error_to_catch:
                    kb = rsa_init(spec)
                except Exception:
                    raise
            else:
                kb = rsa_init(spec)
        elif typ == "EC":
            kb = ec_init(spec)

        for k in kb.keys():
            if kid_template:
                k.kid = kid_template % kid
                kid += 1
            else:
                k.add_kid()
            # kidd[k.use][k.kty] = k.kid

        keyjar.add_kb("", kb)

    return keyjar


def update_keyjar(keyjar):
    for iss, kbl in keyjar.items():
        for kb in kbl:
            kb.update()


def key_summary(keyjar, issuer):
    try:
        kbl = keyjar[issuer]
    except KeyError:
        return ''
    else:
        key_list = []
        for kb in kbl:
            for key in kb.keys():
                if key.inactive_since:
                    key_list.append(
                        '*{}:{}:{}'.format(key.kty, key.use, key.kid))
                else:
                    key_list.append(
                        '{}:{}:{}'.format(key.kty, key.use, key.kid))
        return ', '.join(key_list)


def check_key_availability(inst, jwt):
    """
    If the server is restarted it will NOT load keys from jwks_uris for
    all the clients that has been registered. So this function is there
    to get a clients keys when needed.

    :param inst: OP instance
    :param jwt: A JWT that has to be verified or decrypted
    """

    _rj = jws.factory(jwt)
    payload = json.loads(as_unicode(_rj.jwt.part[1]))
    _cid = payload['iss']
    if _cid not in inst.keyjar:
        cinfo = inst.cdb[_cid]
        inst.keyjar.add_symmetric(_cid, cinfo['client_secret'], ['enc', 'sig'])
        inst.keyjar.add(_cid, cinfo['jwks_uri'])


def public_keys_keyjar(from_kj, origin, to_kj=None, receiver=''):
    """
    Due to cryptography's differentiating between public and private keys
    this function will construct the public equivalent to the private keys
    that a key jar may contain.

    :param from_kj: The KeyJar instance that contains the private keys
    :param origin: The owner ID
    :param to_kj: The KeyJar that is the receiver of the public keys.
    :param receiver: The owner ID under which the public keys should be stored
    :return: The modified KeyJar instance
    """

    if to_kj is None:
        to_kj = KeyJar()

    _jwks = from_kj.export_jwks(issuer=origin)
    to_kj.import_jwks(_jwks, receiver)

    return to_kj


def init_key_jar(public_path='', private_path='', key_defs=''):
    """
    A number of cases here:

    1. A private path is given
       a) The file exists and a JWKS is found there.
          From that JWKS a KeyJar instance is built.
       b)
         If the private path file doesn't exit the key definitions are
         used to build a KeyJar instance. A JWKS with the private keys are
         written to the file named in private_path.
       If a public path is also provided a JWKS with public keys are written
       to that file.
    2. A public path is given but no private path.
       a) If the public path file exists then the JWKS in that file is used to
          construct a KeyJar.
       b) If no such file exists then a KeyJar will be built
          based on the key_defs specification and a JWKS with the public keys
          will be written to the public path file.
    3. If neither a public path nor a private path is given then a KeyJar is
       built based on the key_defs specification and no JWKS will be written
       to file.

    In all cases a KeyJar instance is returned

    The keys stored in the KeyJar will be stored under the '' identifier.

    :param public_path: A file path to a file that contains a JWKS with public
        keys
    :param private_path: A file path to a file that contains a JWKS with
        private keys.
    :param key_defs: A definition of what keys should be created if they are
        not already available
    :return: An instantiated :py:class;`oidcmsg.key_jar.KeyJar` instance
    """

    if private_path:
        if os.path.isfile(private_path):
            _jwks = open(private_path, 'r').read()
            _kj = KeyJar()
            _kj.import_jwks(json.loads(_jwks), '')
        else:
            _kj = build_keyjar(key_defs)
            jwks = _kj.export_jwks(private=True)
            head, tail = os.path.split(private_path)
            if head and not os.path.isdir(head):
                os.makedirs(head)
            fp = open(private_path, 'w')
            fp.write(json.dumps(jwks))
            fp.close()

        if public_path:
            jwks = _kj.export_jwks()  # public part
            fp = open(public_path, 'w')
            fp.write(json.dumps(jwks))
            fp.close()
    elif public_path:
        if os.path.isfile(public_path):
            _jwks = open(public_path, 'r').read()
            _kj = KeyJar()
            _kj.import_jwks(json.loads(_jwks), '')
        else:
            _kj = build_keyjar(key_defs)
            _jwks = _kj.export_jwks()
            head, tail = os.path.split(public_path)
            if head and not os.path.isdir(head):
                os.makedirs(head)
            fp = open(public_path, 'w')
            fp.write(json.dumps(_jwks))
            fp.close()
    else:
        _kj = build_keyjar(key_defs)

    return _kj
