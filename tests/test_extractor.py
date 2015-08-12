import subprocess
import json
import fcntl
import os
import errno

# Mox-esque class that is 'equal' to another string if the value it is
# initialised is contained in that string; used to avoid writing out the
# whole of check_status()
class EqualIfIn:
    def __init__(self, test):
        self.test = test
    def __eq__(self, rhs):
        return isinstance(rhs, basestring) and self.test.lower() in rhs.lower()
    def __repr__(self):
        return "<EqIn " + repr(self.test) + ">"

# from habitat.views.payload_telemetry:
def is_equal_relaxed_floats(a, b):
    """
    Check that a == b, allowing small float differences
    """

    if isinstance(a, list) or isinstance(a, dict):
        # recursion
        if isinstance(a, list):
            if not isinstance(b, list):
                return False
            keys_iter = xrange(len(a))
        else:
            if not isinstance(b, dict):
                return False
            keys_iter = a

        if len(a) != len(b):
            return False

        return all(is_equal_relaxed_floats(a[i], b[i]) for i in keys_iter)

    elif isinstance(a, float) or isinstance(b, float):
        if not (isinstance(a, float) or isinstance(a, int)) or \
           not (isinstance(b, float) or isinstance(b, int)):
            return False

        # fast path
        if a == b:
            return True

        # relaxed float comparison.
        # Doubles provide 15-17 bits of precision. Converting to decimal and
        # back should not introduce an error larger than 1e-15, really.
        tolerance = max(a, b) * 1e-14
        return abs(a - b) < tolerance

    else:
        # string, int, bool, None, ...
        return a == b

class Proxy:
    def __init__(self, command):
        self.closed = False
        self.p = subprocess.Popen(command, stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE)

    def _write(self, command):
        print ">>", repr(command)
        self.p.stdin.write(json.dumps(command))
        self.p.stdin.write("\n")

    def _read(self):
        line = self.p.stdout.readline()
        assert line and line.endswith("\n")
        obj = json.loads(line)

        print "<<", repr(obj)
        return obj

    def check_quiet(self):
        fd = self.p.stdout.fileno()
        fl = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)

        try:
            line = self.p.stdout.readline()
            print ">>", line
        except IOError as e:
            if e.errno != errno.EAGAIN:
                raise
        else:
            # line is '' when EOF; '\n' is an empty line
            if line != '':
                raise AssertionError("expected IOError(EAGAIN), not " +
                                     repr(line))

        fcntl.fcntl(fd, fcntl.F_SETFL, fl)

    def add(self, name):
        self._write(["add", name])

    def skipped(self, num):
        self._write(["skipped", num])

    def push(self, data):
        for char in data:
            self._write(["push", char])

    def set_current_payload(self, value):
        self._write(["set_current_payload", value])

    def check(self, match):
        obj = self._read()
        assert len(obj) >= len(match)
        assert is_equal_relaxed_floats(obj[:len(match)], match)

    def _check_type(self, name, arg):
        if arg:
            self.check([name, arg])
        else:
            self.check([name])

    def check_status(self, message=None):
        if message:
            message = EqualIfIn(message)
        self._check_type("status", message)

    def check_data(self, data=None):
        self._check_type("data", data)

    def check_upload(self, data=None):
        self._check_type("upload", data)

    def __del__(self):
        if not self.closed:
            self.close(check=False)

    def close(self, check=True):
        self.closed = True
        self.p.stdin.close()
        ret = self.p.wait()

        if check:
            self.check_quiet()
            assert ret == 0

class TestExtractorManager:
    def setup(self):
        self.extr = Proxy("tests/extractor")

    def teardown(self):
        self.extr.close()

    def test_management(self):
        self.extr.push("$$this,is,a,string\n")
        self.extr.check_quiet()

        self.extr.add("UKHASExtractor")
        self.extr.push("$$this,is,a,string\n")
        self.extr.check_status("start delim")
        self.extr.check_upload()
        self.extr.check_status("extracted")
        self.extr.check_status("parse failed")
        self.extr.check_data()

class TestUKHASExtractor:
    def setup(self):
        self.extr = Proxy("tests/extractor")
        self.extr.add("UKHASExtractor")

    def teardown(self):
        self.extr.close()

    def test_finds_start_delimiter(self):
        self.extr.push("$")
        self.extr.check_quiet()
        self.extr.push("$")
        self.extr.check_status("start delim")

    def test_extracts(self):
        string = "$$a,simple,test*00\n"
        self.extr.check_quiet()
        self.extr.push(string)
        self.extr.check_status("start delim")
        self.extr.check_upload(string)
        self.extr.check_status("extracted")
        self.extr.check_status("parse failed")
        self.extr.check_data({"_sentence": string})

    def test_can_restart(self):
        self.extr.push("this is some garbage just to mess things up")
        self.extr.check_quiet()
        self.extr.push("$$")
        self.extr.check_status("start delim")

        self.extr.push("garbage: after seeing the delimiter, we lose signal.")
        self.extr.push("some extra $s to con$fuse it $")
        self.extr.push("$$")
        self.extr.check_status("start delim")
        self.extr.check_status("start delim")
        self.extr.check_quiet()
        self.extr.push("helloworld")
        self.extr.check_quiet()
        self.extr.push("\n")
        self.extr.check_upload("$$helloworld\n")
        self.extr.check_status("extracted")
        self.extr.check_status("parse failed")
        self.extr.check_data()

    def test_gives_up_after_1k(self):
        self.extr.push("$$")
        self.extr.check_status("start delim")

        self.extr.push("a" * 1022)
        self.extr.check_status("giving up")
        self.extr.check_quiet()

        # Should have given up, so a \n won't cause an upload:
        self.extr.push("\n")
        self.extr.check_quiet()

        self.test_extracts()

    def test_gives_up_after_50_skipped(self):
        self.extr.push("$$")
        self.extr.check_status("start delim")
        self.extr.skipped(51)
        self.extr.check_status("giving up")
        self.extr.check_quiet()
        self.extr.push("\n")
        self.extr.check_quiet()

    def test_gives_up_after_32_garbage(self):
        self.extr.push("$$")
        self.extr.check_status("start delim")

        self.extr.push("some,legit,data")
        self.extr.push("\t some printable data" * 33)
        self.extr.check_status("giving up")
        self.extr.check_quiet()

        self.extr.push("\n")
        self.extr.check_quiet()

        self.test_extracts()

    def test_skipped(self):
        self.extr.check_quiet()
        self.extr.push("$$some")
        self.extr.check_status("start delim")
        self.extr.skipped(5)
        self.extr.push("data\n")
        self.extr.check_upload("$$somedata\n")
        self.extr.check_status("extracted")
        self.extr.check_status("parse failed")
        self.extr.check_data()

    def basic_data_dict(self, string, callsign):
        return {"_sentence": string, "_parsed": True, "_basic": True,
                "_protocol": "UKHAS", "payload": callsign}

    def check_noconfig(self, string, callsign):
        self.extr.push(string)
        self.extr.check_status("start delim")
        self.extr.check_upload(string)
        self.extr.check_status("extracted")
        self.extr.check_data(self.basic_data_dict(string, callsign))

    def test_crude_parse_noconfig_xor(self):
        self.check_noconfig("$$mypayload,has,a,valid,checksum*1a\n",
                            "mypayload")

    def test_crude_parse_noconfig_crc16_ccitt(self):
        self.check_noconfig("$$mypayload,has,a,valid,checksum*1018\n",
                            "mypayload")

    crude_parse_flight_doc = {
        "sentences": [ {
            "callsign": "TESTING",
            "checksum": "crc16-ccitt",
            "fields": [
                {"name": "field_a"},
                {"name": "field_b"},
                {"name": "field_c"},
                {"name": "int_d", "sensor": "base.ascii_int"},
                {"name": "float_e", "sensor": "base.ascii_float"},
            ],
        } ]
    }

    def test_crude_parse_config(self):
        self.extr.set_current_payload(self.crude_parse_flight_doc)
        string = "$$TESTING,value_a,value_b,value_c,123,453.24*CC76\n"
        self.extr.push(string)
        self.extr.check_status("start delim")
        self.extr.check_upload(string)
        self.extr.check_status("extracted")
        self.extr.check_data({"_sentence": string, "_parsed": True,
                              "_protocol": "UKHAS", "payload": "TESTING",
                              "field_a": "value_a", "field_b": "value_b",
                              "field_c": "value_c", "int_d": 123,
                              "float_e": 453.24})

    def test_crude_checks(self):
        checks = [
            ("$$TESTING,a,b,c*asdfg\n", "invalid checksum len", False),
            ("$$TESTING,a,b,c*45\n", "invalid checksum: expected 1A", False),
            ("$$TESTING,a,b,c*AAAA\n", "invalid checksum: expected BEBC",
                False),
            ("$$TESTING,val_a,val_b*4EB7\n", "incorrect number of fields",
                True),
            ("$$TESTING,a,b,c*1A\n", "wrong checksum type", True),
            ("$$ANOTHER,a,b,c*2355\n", "incorrect callsign", True),
        ]

        self.extr.set_current_payload(self.crude_parse_flight_doc)

        for (string, error, full_parse_line) in checks:
            self.extr.push(string)
            self.extr.check_status("start delim")
            self.extr.check_upload(string)
            self.extr.check_status("extracted")
            if full_parse_line:
                self.extr.check_status("full parse failed:")
            self.extr.check_status(error)
            self.extr.check_data()

    multi_config_flight_doc = {
        "sentences": [
            { "callsign": "AWKWARD",
              "checksum": "crc16-ccitt",
              "fields": [ {"name": "fa"}, {"name": "fo"}, {"name": "fc"} ] },
            { "callsign": "AWKWARD",
              "checksum": "crc16-ccitt",
              "fields": [ {"name": "fa"}, {"name": "fb"} ] }
        ]
    }

    def test_multi_config(self):
        self.extr.set_current_payload(self.multi_config_flight_doc)
        string = "$$AWKWARD,hello,world*D4E9\n"
        self.extr.push(string)
        self.extr.check_status("start delim")
        self.extr.check_upload(string)
        self.extr.check_status("extracted")
        self.extr.check_data({"_sentence": string, "_parsed": True,
                              "_protocol": "UKHAS", "payload": "AWKWARD",
                              "fa": "hello", "fb": "world"})

        string = "$$AWKWARD,extended,other,data*F01F\n"
        self.extr.push(string)
        self.extr.check_status("start delim")
        self.extr.check_upload(string)
        self.extr.check_status("extracted")
        self.extr.check_data({"_sentence": string, "_parsed": True,
                              "_protocol": "UKHAS", "payload": "AWKWARD",
                              "fa": "extended", "fo": "other", "fc": "data"})

    ddmmmmmm_flight_doc = {
        "sentences": [ {
            "callsign": "TESTING",
            "checksum": "crc16-ccitt",
            "fields": [
                {"sensor":"stdtelem.coordinate","name":"lat_a",
                 "format":"dd.dddd"},
                {"sensor":"stdtelem.coordinate","name":"lat_b",
                 "format":"ddmm.mm"},
		{"sensor":"stdtelem.coordinate","name":"lat_a_neg",
		 "format":"ddmm.mm"},
		{"sensor":"stdtelem.coordinate","name":"lat_b_neg",
		 "format":"ddmm.mm"},
                {"name": "field_b"}
            ],
        } ]
    }

    def test_ddmmmmmm(self):
        self.extr.set_current_payload(self.ddmmmmmm_flight_doc)
        string = "$$TESTING,0024.124583,5116.5271,-0016.5271,-5116.5271,whatever*F390\n"
        self.extr.push(string)
        self.extr.check_status("start delim")
        self.extr.check_upload(string)
        self.extr.check_status("extracted")
        self.extr.check_data({"_sentence": string, "_parsed": True,
                              "_protocol": "UKHAS", "payload": "TESTING",
                              "lat_a": "0024.124583", "lat_b": "51.27545",
                              "lat_a_neg": "-0.27545", "lat_b_neg": "-51.27545",
                              "field_b": "whatever" })

    numeric_scale_flight_doc = {
        "sentences": [ {
            "callsign": "TESTING",
            "checksum": "crc16-ccitt",
            "fields": [
                {"sensor":"base.ascii_float","name":"a"},
                {"sensor":"base.ascii_float","name":"b"},
                {"sensor":"base.ascii_float","name":"c"}
            ],
            "filters": {
                "post": [
                    {"filter": "un.related", "type": "normal",
                     "some config": True},
                    {"filter": "common.numeric_scale", "type": "normal",
                     "source": "a", "offset": 6, "factor": 2, "round": 3},
                    {"type": "hotfix", "ignore me": True},
                    {"filter": "common.numeric_scale", "type": "normal",
                     "source": "b", "destination": "b2", "factor": 0.001,
                     "round": 3},
                    {"filter": "common.numeric_scale", "type": "normal",
                     "source": "b", "destination": "b3", "factor": 5}
                ]
            }
        } ]
    }

    def test_numeric_scale(self):
        self.extr.set_current_payload(self.numeric_scale_flight_doc)
        string = "$$TESTING,100.123,0.00482123,48*60A4\n"
        self.extr.push(string)
        self.extr.check_status("start delim")
        self.extr.check_upload(string)
        self.extr.check_status("extracted")
        self.extr.check_data({"_sentence": string, "_parsed": True,
                              "_protocol": "UKHAS", "payload": "TESTING",
                              "a": 206, "b": 0.00482123, "b2": 0.00000482,
                              "b3": 0.00482123 * 5, "c": 48})
