SHELL=/bin/bash

# Every phony target must directly or indirectly depend on this target. This
# implies that only targets that have no other dependencies must explicitly
# list this target as a dependency:
#
.PHONY: check_env
check_env:
	@if ! test -n "$$project_root"; then \
		echo -e "\nPlease run 'source environment' from the project root\n"; \
		false; \
	fi

.PHONY: check_venv
check_venv: check_env
	@if ! test -n "$$VIRTUAL_ENV"; then \
		echo -e "\nError: Run 'source .venv/bin/activate' first\n"; \
		false; \
	fi

.PHONY: check_python
check_python: check_venv
	@if test "$$VIRTUAL_ENV/bin/python" != "$$(hash python && hash -t python)"; then \
  		echo -e "\nPATH lookup yields a 'python' executable from outside the virtualenv\n"; \
		false; \
	fi
	@if test "$$VIRTUAL_ENV/bin/pip" != "$$(hash pip && hash -t pip)"; then \
  		echo -e "\nPATH lookup yields a 'pip' executable from outside the virtualenv\n"; \
		false; \
	fi
	@if ! python -c "import sys; sys.exit(0 if '.'.join(map(str, sys.version_info[:3])) == '${azul_python_version}' else 1)"; then \
		echo -e "\nLooks like Python ${azul_python_version} is not installed or active in the current virtualenv\n"; \
		false; \
	fi
	@if ! python -c "import sys; exec('try: import chalice\nexcept: sys.exit(1)\nelse: sys.exit(0)')"; then \
		echo -e "\nLooks like some requirements are missing. Please run 'make requirements'\n"; \
		false; \
	fi
	@if ! python -c "import sys, wheel as w; \
		           from pkg_resources import parse_version as p; \
		           sys.exit(0 if p(w.__version__) >= p('0.32.3') else 1)"; then \
		echo -e "\nLooks like the `wheel` package is outdated or missing. See README for instructions on how to fix this.\n"; \
		false; \
	fi
	@if ! python -c "import sys; \
                     from chalice import chalice_version as v; \
		             from pkg_resources import parse_version as p; \
		             sys.exit(0 if p(v) == p('1.29.0') else 1)"; then \
		echo -e "\nLooks like chalice is out of date. Please run 'make requirements'\n"; \
		false; \
	fi

.PHONY: check_terraform
check_terraform: check_env
	@if ! hash terraform; then \
		echo -e "\nLooks like Terraform is not installed.\n"; \
		false; \
	fi
	@if test "$$(terraform --version --json \
	             | python -c 'import json; print(json.load(open(0))["terraform_version"])')" \
	         != \
	         "$$azul_terraform_version"; then \
		echo -e "\nLooks like the wrong version of Terraform is installed.\n"; \
		false; \
	fi

.PHONY: check_docker
check_docker:
	@if ! hash docker; then \
		echo -e "\nLooks like Docker is not installed.\n"; \
		false; \
	fi

.PHONY: check_aws
check_aws: check_python
	@if ! python -c "import os, sys, boto3 as b; \
		sys.exit(0 if os.environ.get('TRAVIS') == 'true' or \
		         b.client('sts').get_caller_identity()['Account'] == os.environ['AZUL_AWS_ACCOUNT_ID'] else 1)"; then \
		echo -e "\nLooks like there is a mismatch between AZUL_AWS_ACCOUNT_ID and the currently active AWS credentials. \
		         \nCheck the output from 'aws sts get-caller-identity' against the value of that environment variable.\n"; \
		false; \
	fi

.PHONY: check_branch
check_branch: check_python
	python $(project_root)/scripts/check_branch.py

.PHONY: git_clean_recursive
git_clean_recursive: check_env
	git clean -Xdf

.PHONY: git_list_dirt_recursive
git_list_dirt_recursive: check_env
	git clean -Xdn

# `grep` returns 1 if nothing matches. Here, this does not constitute an error.
define list_dirt
	set -o pipefail \
	&& git ls-files --ignored --others --directory --exclude-standard \
		| (grep -v '/[^/]' || test $$? -eq 1)
endef

.PHONY: git_clean
git_clean: check_env
	$(call list_dirt) | xargs -r rm -rv

.PHONY: git_list_dirt
git_list_dirt: check_env
	@$(call list_dirt)

%.json: %.json.template.py check_python .FORCE
	python $< $@
.FORCE:

# The template output file depends on the template file, of course, as well as
# the environment. To be safe we force the template creation. This is what
# the fake .FORCE target does. It still is necessary to declare a target's
# dependency on a template to ensure correct ordering.
