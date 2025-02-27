# Copyright The OpenTelemetry Authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from unittest.mock import Mock, patch

from flask import Flask, request

from opentelemetry import trace
from opentelemetry.instrumentation.flask import FlaskInstrumentor
from opentelemetry.instrumentation.propagators import (
    TraceResponsePropagator,
    get_global_response_propagator,
    set_global_response_propagator,
)
from opentelemetry.semconv.trace import SpanAttributes
from opentelemetry.test.test_base import TestBase
from opentelemetry.test.wsgitestutil import WsgiTestBase
from opentelemetry.util.http import get_excluded_urls

# pylint: disable=import-error
from .base_test import InstrumentationTest


def expected_attributes(override_attributes):
    default_attributes = {
        SpanAttributes.HTTP_METHOD: "GET",
        SpanAttributes.HTTP_SERVER_NAME: "localhost",
        SpanAttributes.HTTP_SCHEME: "http",
        SpanAttributes.NET_HOST_PORT: 80,
        SpanAttributes.HTTP_HOST: "localhost",
        SpanAttributes.HTTP_TARGET: "/",
        SpanAttributes.HTTP_FLAVOR: "1.1",
        SpanAttributes.HTTP_STATUS_CODE: 200,
    }
    for key, val in override_attributes.items():
        default_attributes[key] = val
    return default_attributes


class TestProgrammatic(InstrumentationTest, TestBase, WsgiTestBase):
    def setUp(self):
        super().setUp()

        self.app = Flask(__name__)

        FlaskInstrumentor().instrument_app(self.app)

        self._common_initialization()

        self.env_patch = patch.dict(
            "os.environ",
            {
                "OTEL_PYTHON_FLASK_EXCLUDED_URLS": "http://localhost/excluded_arg/123,excluded_noarg"
            },
        )
        self.env_patch.start()
        self.exclude_patch = patch(
            "opentelemetry.instrumentation.flask._excluded_urls",
            get_excluded_urls("FLASK"),
        )
        self.exclude_patch.start()

    def tearDown(self):
        super().tearDown()
        self.env_patch.stop()
        self.exclude_patch.stop()
        with self.disable_logging():
            FlaskInstrumentor().uninstrument_app(self.app)

    def test_uninstrument(self):
        resp = self.client.get("/hello/123")
        self.assertEqual(200, resp.status_code)
        self.assertEqual([b"Hello: 123"], list(resp.response))
        span_list = self.memory_exporter.get_finished_spans()
        self.assertEqual(len(span_list), 1)

        FlaskInstrumentor().uninstrument_app(self.app)

        resp = self.client.get("/hello/123")
        self.assertEqual(200, resp.status_code)
        self.assertEqual([b"Hello: 123"], list(resp.response))
        span_list = self.memory_exporter.get_finished_spans()
        self.assertEqual(len(span_list), 1)

    # pylint: disable=no-member
    def test_only_strings_in_environ(self):
        """
        Some WSGI servers (such as Gunicorn) expect keys in the environ object
        to be strings

        OpenTelemetry should adhere to this convention.
        """
        nonstring_keys = set()

        def assert_environ():
            for key in request.environ:
                if not isinstance(key, str):
                    nonstring_keys.add(key)
            return "hi"

        self.app.route("/assert_environ")(assert_environ)
        self.client.get("/assert_environ")
        self.assertEqual(nonstring_keys, set())

    def test_simple(self):
        expected_attrs = expected_attributes(
            {
                SpanAttributes.HTTP_TARGET: "/hello/123",
                SpanAttributes.HTTP_ROUTE: "/hello/<int:helloid>",
            }
        )
        self.client.get("/hello/123")

        span_list = self.memory_exporter.get_finished_spans()
        self.assertEqual(len(span_list), 1)
        self.assertEqual(span_list[0].name, "/hello/<int:helloid>")
        self.assertEqual(span_list[0].kind, trace.SpanKind.SERVER)
        self.assertEqual(span_list[0].attributes, expected_attrs)

    def test_trace_response(self):
        orig = get_global_response_propagator()

        set_global_response_propagator(TraceResponsePropagator())
        response = self.client.get("/hello/123")
        headers = response.headers

        span_list = self.memory_exporter.get_finished_spans()
        self.assertEqual(len(span_list), 1)
        span = span_list[0]

        self.assertIn("traceresponse", headers)
        self.assertEqual(
            headers["access-control-expose-headers"], "traceresponse",
        )
        self.assertEqual(
            headers["traceresponse"],
            "00-{0}-{1}-01".format(
                trace.format_trace_id(span.get_span_context().trace_id),
                trace.format_span_id(span.get_span_context().span_id),
            ),
        )

        set_global_response_propagator(orig)

    def test_not_recording(self):
        mock_tracer = Mock()
        mock_span = Mock()
        mock_span.is_recording.return_value = False
        mock_tracer.start_span.return_value = mock_span
        with patch("opentelemetry.trace.get_tracer") as tracer:
            tracer.return_value = mock_tracer
            self.client.get("/hello/123")
            self.assertFalse(mock_span.is_recording())
            self.assertTrue(mock_span.is_recording.called)
            self.assertFalse(mock_span.set_attribute.called)
            self.assertFalse(mock_span.set_status.called)

    def test_404(self):
        expected_attrs = expected_attributes(
            {
                SpanAttributes.HTTP_METHOD: "POST",
                SpanAttributes.HTTP_TARGET: "/bye",
                SpanAttributes.HTTP_STATUS_CODE: 404,
            }
        )

        resp = self.client.post("/bye")
        self.assertEqual(404, resp.status_code)
        resp.close()
        span_list = self.memory_exporter.get_finished_spans()
        self.assertEqual(len(span_list), 1)
        self.assertEqual(span_list[0].name, "HTTP POST")
        self.assertEqual(span_list[0].kind, trace.SpanKind.SERVER)
        self.assertEqual(span_list[0].attributes, expected_attrs)

    def test_internal_error(self):
        expected_attrs = expected_attributes(
            {
                SpanAttributes.HTTP_TARGET: "/hello/500",
                SpanAttributes.HTTP_ROUTE: "/hello/<int:helloid>",
                SpanAttributes.HTTP_STATUS_CODE: 500,
            }
        )
        resp = self.client.get("/hello/500")
        self.assertEqual(500, resp.status_code)
        resp.close()
        span_list = self.memory_exporter.get_finished_spans()
        self.assertEqual(len(span_list), 1)
        self.assertEqual(span_list[0].name, "/hello/<int:helloid>")
        self.assertEqual(span_list[0].kind, trace.SpanKind.SERVER)
        self.assertEqual(span_list[0].attributes, expected_attrs)

    def test_exclude_lists(self):
        self.client.get("/excluded_arg/123")
        span_list = self.memory_exporter.get_finished_spans()
        self.assertEqual(len(span_list), 0)

        self.client.get("/excluded_arg/125")
        span_list = self.memory_exporter.get_finished_spans()
        self.assertEqual(len(span_list), 1)

        self.client.get("/excluded_noarg")
        span_list = self.memory_exporter.get_finished_spans()
        self.assertEqual(len(span_list), 1)

        self.client.get("/excluded_noarg2")
        span_list = self.memory_exporter.get_finished_spans()
        self.assertEqual(len(span_list), 1)


class TestProgrammaticCustomSpanName(
    InstrumentationTest, TestBase, WsgiTestBase
):
    def setUp(self):
        super().setUp()

        def custom_span_name():
            return "flask-custom-span-name"

        self.app = Flask(__name__)

        FlaskInstrumentor().instrument_app(
            self.app, name_callback=custom_span_name
        )

        self._common_initialization()

    def tearDown(self):
        super().tearDown()
        with self.disable_logging():
            FlaskInstrumentor().uninstrument_app(self.app)

    def test_custom_span_name(self):
        self.client.get("/hello/123")

        span_list = self.memory_exporter.get_finished_spans()
        self.assertEqual(len(span_list), 1)
        self.assertEqual(span_list[0].name, "flask-custom-span-name")


class TestProgrammaticCustomSpanNameCallbackWithoutApp(
    InstrumentationTest, TestBase, WsgiTestBase
):
    def setUp(self):
        super().setUp()

        def custom_span_name():
            return "instrument-without-app"

        FlaskInstrumentor().instrument(name_callback=custom_span_name)
        # pylint: disable=import-outside-toplevel,reimported,redefined-outer-name
        from flask import Flask

        self.app = Flask(__name__)

        self._common_initialization()

    def tearDown(self):
        super().tearDown()
        with self.disable_logging():
            FlaskInstrumentor().uninstrument()

    def test_custom_span_name(self):
        self.client.get("/hello/123")

        span_list = self.memory_exporter.get_finished_spans()
        self.assertEqual(len(span_list), 1)
        self.assertEqual(span_list[0].name, "instrument-without-app")
