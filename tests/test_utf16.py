from collector.entities.utf16 import utf16_len, utf16_slice, truncate_to_utf16


class TestUtf16Len:

    def test_ascii(self):
        assert utf16_len('hello') == 5

    def test_cyrillic(self):
        assert utf16_len('привет') == 6

    def test_emoji_guitar(self):
        assert utf16_len('🎸') == 2

    def test_emoji_fire(self):
        assert utf16_len('🔥') == 2

    def test_mixed_cyrillic_emoji(self):
        assert utf16_len('Sirenia 🎸, 20:00') == 17

    def test_mixed_ascii_emoji(self):
        assert utf16_len('a🎸b') == 4

    def test_empty(self):
        assert utf16_len('') == 0

    def test_newline(self):
        assert utf16_len('\n') == 1

    def test_bullet(self):
        assert utf16_len('• ') == 2

    def test_multiple_emoji(self):
        assert utf16_len('🎸🔥') == 4


class TestUtf16Slice:

    def test_ascii_slice(self):
        assert utf16_slice('hello world', 0, 5) == 'hello'

    def test_cyrillic_slice(self):
        assert utf16_slice('привет мир', 0, 6) == 'привет'

    def test_slice_with_emoji(self):
        assert utf16_slice('ab🎸cd', 0, 4) == 'ab🎸'

    def test_slice_after_emoji(self):
        assert utf16_slice('ab🎸cd', 4, 6) == 'cd'

    def test_empty_slice(self):
        assert utf16_slice('hello', 2, 2) == ''


class TestTruncateToUtf16:

    def test_no_truncation_needed(self):
        assert truncate_to_utf16('hello', 10) == 'hello'

    def test_truncate_ascii(self):
        assert truncate_to_utf16('hello world', 5) == 'hello'

    def test_truncate_cyrillic(self):
        assert truncate_to_utf16('привет мир', 6) == 'привет'

    def test_truncate_before_emoji(self):
        assert truncate_to_utf16('ab🎸cd', 3) == 'ab'

    def test_truncate_includes_emoji(self):
        assert truncate_to_utf16('ab🎸cd', 4) == 'ab🎸'

    def test_truncate_empty(self):
        assert truncate_to_utf16('', 10) == ''

    def test_exact_limit(self):
        assert truncate_to_utf16('hello', 5) == 'hello'