import io
import json
import logging
import unittest
from contextlib import redirect_stderr

import run_log


class RunLogTests(unittest.TestCase):
    def lines(self, emit):
        """What ``emit`` logs, as the container's log holds it."""
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(run_log.JsonLines())
        logger = logging.getLogger("test_run_log")
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        self.addCleanup(logger.removeHandler, handler)
        emit(logger)
        return [json.loads(line) for line in stream.getvalue().splitlines()]

    def test_an_error_is_logged_by_its_type_and_place_never_its_words(self):
        def emit(logger):
            try:
                raise ValueError("Alex Example built REST APIs at Acme")
            except ValueError:
                run_log.event(logger, "request", level=logging.ERROR, error=True, status=500)
        [line] = self.lines(emit)
        self.assertEqual((line["level"], line["event"], line["status"], line["error"]), ("error", "request", 500, "ValueError"))
        self.assertRegex(line["at"], r"^test_run_log\.py:\d+$")
        self.assertNotIn("Alex", json.dumps(line))

    def test_a_library_line_keeps_its_wording_only_when_it_is_known_fixed_wording(self):
        def emit(logger):
            logger.warning("parse failed: %s", ValueError("Alex Example built REST APIs"))  # values come separately
            logger.warning("parse failed: Alex Example built REST APIs")  # put together before it was logged
            logger.warning("parse failed for Alex Example: %s", "no dates")  # both at once
            try:
                raise KeyError("Built REST APIs for an internal tool.")
            except KeyError:
                logger.exception("Exception in ASGI application\n")  # what uvicorn logs when a request fails
            logger.info("Started server process [%d]", 4242)
        lines = self.lines(emit)
        self.assertEqual([line["message"] for line in lines],
                         [run_log.WITHHELD, run_log.WITHHELD, run_log.WITHHELD,
                          "Exception in ASGI application", "Started server process [%d]"])
        self.assertEqual(lines[3]["error"], "KeyError")
        for words in ("Alex", "REST", "Traceback", "4242"):
            self.assertNotIn(words, json.dumps(lines))

    def test_a_library_line_logged_during_a_request_carries_its_id(self):
        current = run_log.request_id.set("0123456789abcdef")
        try:
            [line] = self.lines(lambda logger: logger.error("Exception in ASGI application\n"))
        finally:
            run_log.request_id.reset(current)
        self.assertEqual(line["request_id"], "0123456789abcdef")

    def test_an_event_carries_the_request_it_was_logged_in(self):
        current = run_log.request_id.set("0123456789abcdef")
        try:
            [line] = self.lines(lambda logger: run_log.event(logger, "stage", stage="rewording", status="fallback",
                                                              reason="rate_limited"))
        finally:
            run_log.request_id.reset(current)
        self.assertEqual({key: line[key] for key in ("request_id", "event", "stage", "status", "reason")},
                         {"request_id": "0123456789abcdef", "event": "stage", "stage": "rewording",
                          "status": "fallback", "reason": "rate_limited"})

    def test_on_a_terminal_an_event_reads_as_words_and_fields(self):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger = logging.getLogger("test_run_log.plain")
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        self.addCleanup(logger.removeHandler, handler)
        run_log.event(logger, "request", method="GET", path="/api/facts", status=200, duration_ms=3)
        self.assertEqual(stream.getvalue(), "request method=GET path=/api/facts status=200 duration_ms=3\n")

    def test_nothing_is_printed_where_no_one_has_set_up_logging(self):
        printed = io.StringIO()
        with redirect_stderr(printed):
            run_log.event(logging.getLogger("workbench.stages"), "stage", level=logging.WARNING, status="fallback")
        self.assertEqual(printed.getvalue(), "")

    def test_set_up_for_a_container_every_line_is_json_even_a_library_traceback(self):
        root = logging.getLogger()
        saved = (root.handlers[:], root.level)
        self.addCleanup(lambda: (root.handlers.clear(), root.handlers.extend(saved[0]), root.setLevel(saved[1])))
        stream = io.StringIO()
        run_log.configure(stream)
        try:
            raise KeyError("Built REST APIs for an internal tool.")
        except KeyError:
            logging.getLogger("uvicorn.error").exception("Exception in ASGI application")
        logging.getLogger("httpx").info("HTTP Request: GET https://boards-api.greenhouse.io/v1/boards/example/jobs")
        run_log.event(logging.getLogger("workbench"), "listening", port=8765)
        lines = [json.loads(line) for line in stream.getvalue().splitlines()]
        self.assertEqual([(line.get("event"), line.get("error")) for line in lines],
                         [(None, "KeyError"), ("listening", None)])  # a library's routine chatter is left out
        self.assertNotIn("REST", stream.getvalue())


if __name__ == "__main__":
    unittest.main()
