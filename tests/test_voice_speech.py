from primer.voice.speech import chunks, speakable


def test_speakable_numbers_and_markup():
    s = speakable
    # years and numbers are spelled out
    assert s("In 1068 he came.") == "In ten sixty-eight he came."
    assert s("In 1604 it fell.") == "In sixteen oh four it fell."
    assert s("In 2005 it ended.") == "In two thousand and five it ended."
    assert s("In 2024.") == "In twenty twenty-four."
    assert s("the 12th century") == "the twelfth century"
    assert s("the 21st century") == "the twenty-first century"
    assert s("3,500 men") == "three thousand five hundred men"
    assert s("100,000 men") == "one hundred thousand men"
    assert s("112 men") == "one hundred and twelve men"
    assert s("AD 43") == "AD forty-three"
    assert s("c. 1100") == "circa eleven hundred"
    assert s("the 1990s") == "the nineteen nineties"
    assert s("the 1900s") == "the nineteen hundreds"
    assert s("2.5 miles") == "two point five miles"
    assert s("5% of it") == "five percent of it"
    assert s("1066-1087") == "ten sixty-six to ten eighty-seven"
    # Markdown, citations and links never reach the voice
    assert s("**Bold** and _italic_ and `code`.") == "Bold and italic and code."
    assert s("# Title\n\nSome text[3] here[^1], see [the page](http://x.org/a)[2].") \
        == "Title. Some text here, see the page."
    assert s("Visit https://example.com/x?y=1 now.") == "Visit now."
    assert s("- one\n- two\n\n1. three") == "one. two. three"
    assert s("![a picture](img.png)Text") == "Text"
    assert s("snake_case_name stays") == "snake_case_name stays"
    assert s("  many   spaces\nand lines \n\n\nNext one") == "many spaces and lines. Next one"


def test_chunks_sentence_and_limit():
    sent = "The quick brown fox jumps over the lazy dog and keeps running far. "
    text = (sent * 11).strip()
    assert len(text) > 700
    rs = chunks(text)
    assert all(0 < e - b <= 300 for b, e in rs)
    assert len(rs) == 3  # sentences packed up to the limit: 4 + 4 + 3
    assert "".join(text[b:e] for b, e in rs).replace(" ", "") == text.replace(" ", "")
    assert all(text[b] != " " and text[e - 1] != " " for b, e in rs)
    # one overlong sentence is split at commas, then at spaces
    long = ("word, " * 100).strip(", ") + "."
    rs = chunks(long, 100)
    assert all(e - b <= 100 for b, e in rs) and len(rs) > 1
    assert "".join(long[b:e] for b, e in rs).replace(" ", "") == long.replace(" ", "")
    nospace = "x" * 650
    assert [e - b for b, e in chunks(nospace)] == [300, 300, 50]
    # abbreviations and initials don't end a sentence
    assert len(chunks("Dr. Smith met J. R. Tolkien. They talked.", 30)) == 2
    assert chunks("") == [] and chunks("   ") == []


def test_speakable_money_units_ids_and_odd_numbers():
    s = speakable
    assert s("\u00a35") == "five pounds" and s("\u00a31") == "one pound"
    assert s("$5.50") == "five dollars fifty" and s("\u20ac3") == "three euros"
    assert s("\u00a35m") == "five million pounds" and s("$2bn") == "two billion dollars"
    assert s("\u00a31,500") == "one thousand five hundred pounds"
    assert s("5km") == "five kilometres" and s("10kg") == "ten kilograms"
    # ranges are exactly two numbers; IDs are spelled digit by digit
    assert s("pages 5-10") == "pages five to ten"
    assert s("555-1234") == "five five five, one two three four"
    assert s("555-123-4567") == "five five five, one two three, four five six seven"
    # versions and dates are never half spelled
    assert s("version 2.0.1") == "version two dot zero dot one"
    assert s("12.5.2020") == "one two dot five dot two zero two zero"
    assert s("it was -3.5 today") == "it was minus three point five today"
    assert s("-5 degrees") == "minus five degrees"
    assert s("the 1960's") == "the nineteen sixties"
    assert s("1 000 men") == "one thousand men"
    # a year, unless a counted thing follows
    assert s("in 1250 he") == "in twelve fifty he"
    assert s("1250 soldiers and 1500 miles") == (
        "one thousand two hundred and fifty soldiers and one thousand five hundred miles")
    # single asterisks are emphasis only at word boundaries
    assert s("2*3*4 and a*b*c") == "two*three*four and a*b*c"
    assert s("*emph* word") == "emph word"


def test_speakable_unit_abbreviations():
    s = speakable
    assert s("30m high") == "thirty metres high" and s("30 m") == "thirty m"
    assert s("5km") == "five kilometres" and s("10kg") == "ten kilograms"
    assert s("12cm and 3mm") == "twelve centimetres and three millimetres"
    assert s("200g") == "two hundred grams" and s("200 g") == "two hundred g" and s("6ft") == "six feet"
    assert s("a 5in nail") == "a five inches nail" and s("60mph") == "sixty miles per hour"
    assert s("3 mi away") == "three miles away"
    assert s("1 km") == "one kilometre" and s("1m") == "one metre"
    assert s("30°C") == "thirty degrees Celsius"
    assert s("86 °F") == "eighty-six degrees Fahrenheit"
    assert s("-5°C") == "minus five degrees Celsius"
    assert s("£5m") == "five million pounds"
    # only after a number
    assert s("in the m group") == "in the m group" and s("sat in 5 minutes") == "sat in five minutes"
    assert s("3 in the house") == "three in the house"
    assert s("1250km") == "one thousand two hundred and fifty kilometres"
