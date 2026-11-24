"""Fixed allergy history in the clinical record."""

import re

LONGITUDINAL_POLICY = 'longitudinal_allergy_history_v1'
PATIENT_TOPIC = 'allergy_history'
DOCUMENTED_TERMS = re.compile(r'allerg|hypersensitiv|anaphyla|adverse (?:drug )?reaction|过敏|不良反应', re.I)
MEDICATION_HISTORY_QUESTION = re.compile(r'(?:home|prior|previous|current|what) (?:drugs|medications|medicines)|'
                                         r'平时.{0,6}(?:用药|吃药)|既往用药|以前.{0,6}(?:用药|吃药)', re.I)
REACTION_QUESTION = re.compile(r'what (?:happens|happened)|which reaction|what kind of reaction|'
                               r'具体.{0,6}反应|什么反应|怎么反应|什么症状|哪里不舒服', re.I)


def patient_statement(fact, mode):
    text = fact['value'].strip('。')
    if mode == 'reaction' and fact.get('allergens'):
        known = [f"{row['name']}会让我{row['reaction']}" for row in fact['allergens'] if row.get('reaction')]
        unknown = [row['name'] for row in fact['allergens'] if not row.get('reaction')]
        sentences = ['；'.join(known)] if known else []
        if unknown:
            sentences.append('、'.join(unknown) + '的具体反应我说不清')
        text = '。'.join(sentences)
    if mode == 'volunteer':
        text = '对了，' + text
    return text + '。'


def documentation_only(question):
    """Recognize requests to establish undocumented allergy history."""
    text = re.sub(r'[_-]+', ' ', question.casefold())
    if not DOCUMENTED_TERMS.search(text):
        return False
    if re.search(r'(?:documented|reported|known|confirmed|prior|previous)\s+(?:\w+\s+){0,3}'
                 r'(?:reaction|anaphyla\w*|allergy to)|\ballergic to\b|'
                 r'cross reactiv|(?:incorrect|excessive|missing)\s+(?:dose|route)|'
                 r'interaction|重复|剂量不|过敏反应已', text):
        return False
    return bool(re.search(r'\b(?:unknown|undocumented|unconfirmed|unverified|unclear|missing|'
                          r'clarif\w*|confirm\w*|verif\w*|ascertain|incomplete)\b|'
                          r'lack of|no (?:documented|recorded) (?:drug )?allerg|'
                          r'过敏史.{0,12}(?:未|不明|待|不详)|(?:核实|确认).{0,12}过敏史', text))
