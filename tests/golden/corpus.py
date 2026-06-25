# Each case: (messages_in, known_real_values_that_must_NOT_appear_outbound)
# IDs below are valid Israeli IDs (pass the check-digit): 123456709, 765432109.
GAZETTEER = [("ישראל ישראלי", "לקוח"), ("פלוני אלמוני", "לקוח"),
             ("שירה לוי", "לקוח"), ("John Doe", "לקוח")]

CASES = [
    (
        [{"role": "user", "content":
          'סכם את המכתב לחייבים ישראל ישראלי (ת"ז 123456709) ופלוני אלמוני, '
          'תיק פש"ר 100200300, טלפון 050-1234567, מייל debtor@example.co.il'}],
        ["ישראל ישראלי", "פלוני אלמוני", "123456709", "100200300", "050-1234567", "debtor@example.co.il"],
    ),
    (
        [{"role": "user", "content": "שלח מייל ל-John Doe על התיק של שירה לוי"}],
        ["John Doe", "שירה לוי"],
    ),
]
