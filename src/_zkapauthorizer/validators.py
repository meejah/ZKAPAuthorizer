# Copyright 2019 PrivateStorage.io, LLC
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

"""
This module implements validators for ``attrs``-defined attributes.
"""

from base64 import b64decode


def is_base64_encoded(b64decode=b64decode):
    """
    Return a a attrs validator that verifies that the attributes is a base64
    encoded byte string.
    """

    def validate_is_base64_encoded(inst, attr, value):
        try:
            b64decode(value)
        except TypeError:
            raise TypeError(
                "{name!r} must be base64 encoded bytes, (got {value!r})".format(
                    name=attr.name,
                    value=value,
                ),
            )

    return validate_is_base64_encoded


def has_length(expected):
    def validate_has_length(inst, attr, value):
        if len(value) != expected:
            raise ValueError(
                "{name!r} must have length {expected}, instead has length {actual}".format(
                    name=attr.name,
                    expected=expected,
                    actual=len(value),
                ),
            )

    return validate_has_length


def greater_than(expected):
    def validate_relation(inst, attr, value):
        if value > expected:
            return None

        raise ValueError(
            "{name!r} must be greater than {expected}, instead it was {actual}".format(
                name=attr.name,
                expected=expected,
                actual=value,
            ),
        )

    return validate_relation
