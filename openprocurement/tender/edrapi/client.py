import requests


class EdrApiClient(object):
    """Base class for making requests to EDR"""

    def __init__(self):
        self.token = '06a39f787e43625eff0eb288ff37a7b17f2fef52'
        self.url = 'https://zqedr-api.nais.gov.ua/1.0/subjects'
        self.headers = {"Accept": "application/json", 'Authorization': 'Token {}'.format(self.token)}

    def get_by_code(self, code):
        param = code.isdigit() and len(code) < 13 and 'code' or 'passport'  # find out we accept edrpou or passport code
        url = '{url}?{param}={code}'.format(url=self.url, param=param, code=code)
        response = requests.get(url=url, headers=self.headers)

        return response
