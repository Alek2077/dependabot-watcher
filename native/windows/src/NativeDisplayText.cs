using System.Globalization;
using System.Text;

namespace WatcherNativeProof;

// Presentation only: original context, digests and messages stay unchanged.
// Invisible formatting can disguise advice, filenames and commands in a plain
// TextBlock just as readily as in a terminal. Preserve ordinary Unicode/text.
internal static class NativeDisplayText
{
    internal static string Escape(string text)
    {
        var rendered = new StringBuilder(text.Length);
        foreach (var rune in text.EnumerateRunes())
        {
            var category = Rune.GetUnicodeCategory(rune);
            if (rune.Value != '\n' && (category is UnicodeCategory.Control or UnicodeCategory.Format))
                rendered.Append(rune.Value <= 0xffff ? $"\\u{rune.Value:x4}" : $"\\U{rune.Value:x8}");
            else rendered.Append(rune.ToString());
        }
        return rendered.ToString();
    }
}
