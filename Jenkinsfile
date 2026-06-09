// PROJECT SETTINGS
def IMAGE_NAME = "rtc-optimizer"
def PYTHON_PACKAGE_NAME = "rtc-optimizer"
def DOCKER_REGISTRY_NAME = "vertexinternalcontainers"
def CI_REGISTRY = "${DOCKER_REGISTRY_NAME}.azurecr.io"
def DOCKER_REGISTRY = "${CI_REGISTRY}/docker/vertex-cloudems/"
def DOCKER_FILE_PATH = "."
def SONARQUBE_PROJECT_NAME = "Vertex-CloudEMS-RTC-optimizer"
def SONARQUBE_HOST = "https://sonarqube.eehc.nl"
def COVERAGE_REPORT = "coverage.xml"
def DOCKER_CONTAINER_REGISTRY_URL = "https://index.docker.io/v1/"
def GITOPS_REPO_URL = "https://github.com/EnergyEssentials/vtx-config.git"
def GITOPS_MODULE_KEY = "vertex-cloudems-rtc-optimizer"

// BUILD PARAMETERS
def VERSION = "0.0.1"
def CONFIG_BRANCH_NAME = "V0.0.1"
def COMMITHASH = "UNKNOWN"
def CI_REGISTRY_ACCESS_KEY = "${DOCKER_REGISTRY_NAME}_base64_accesskey"

def sendFailureMessageOnSlack(details) {
    slackSend(
        channel: '#vtx-jenkins',
        message: "CloudEMS RTC Optimizer Pipeline fail. Job: '${env.JOB_NAME} [${env.BUILD_NUMBER}]' - ${env.BUILD_URL}. Details: ${details}",
        color: '#FF0000'
    )
}

def sendSucceedMessageOnSlack() {
    slackSend(
        channel: '#vtx-jenkins',
        message: "CloudEMS RTC Optimizer Pipeline succeed. Job: '${env.JOB_NAME} [${env.BUILD_NUMBER}]' - ${env.BUILD_URL}.",
        color: '#36a64f'
    )
}

podTemplate(label: 'rtc-optimizer-build-pod', cloud: 'kubernetes', serviceAccount: 'vertex',
  yaml: """
  apiVersion: v1
  kind: Pod
  spec:
    tolerations:
    - key: "k8s.scaleway.com/jenkins-agents-only"
      operator: "Exists"
      effect: "NoSchedule"
    containers:
      - name: buildkit
        image: moby/buildkit:v0.11.6
        tty: true
        stdin: true
        command: ['buildkitd']
        args: ['--addr', 'unix:///run/buildkit/buildkitd.sock', '--addr', 'tcp://0.0.0.0:1234']
        securityContext:
          privileged: true
      - name: python
        image: python:3.13
        tty: true
        stdin: true
        command: ['cat']
        resources:
          requests:
            memory: "8Gi"
          limits:
            memory: "12Gi"
  """,
  volumes: []) {
    node('rtc-optimizer-build-pod') {
        try {
            stage('Checkout source') {
                scm_result = checkout scm
                COMMITHASH = scm_result.get('GIT_COMMIT')
                env.SCM_CRED_ID = scm.userRemoteConfigs[0].credentialsId
            }

            stage('Determine and write version') {
                echo "Determining version from branch name ${BRANCH_NAME}"

                def versionNumber = (BRANCH_NAME =~ /V(\d*[.]\d*[.]\d*)/)[0][1]

                CONFIG_BRANCH_NAME = "V${versionNumber}"
                VERSION = "${versionNumber}.${currentBuild.id}"

                currentBuild.displayName = "${VERSION}"
                currentBuild.description = "Build of CloudEMS RTC Optimizer version ${VERSION}"

                def gitCommitShortCode = COMMITHASH[0..7]
                def informationalVersion = "V${VERSION} (${gitCommitShortCode})"
                def buildInfo = [
                    GitRepo: 'rtc-tools-bess-demo',
                    BuildId: currentBuild.id,
                    Version: VERSION,
                    GitCommit: COMMITHASH,
                    GitShortCommit: gitCommitShortCode,
                    InformationalVersion: informationalVersion
                ]

                // Write build_info.json for runtime version introspection
                writeFile file: 'build_info.json', text: groovy.json.JsonOutput.toJson(buildInfo)

                // Write healthz.json for ClusterScout-compatible health endpoint
                def healthzContent = groovy.json.JsonOutput.toJson([
                    status: "Healthy",
                    totalDuration: "00:00:00.0000001",
                    entries: [
                        VersionCheck: [
                            data: [:],
                            description: informationalVersion,
                            duration: "00:00:00.0000001",
                            status: "Healthy",
                            tags: ["VersionProviderCheck"]
                        ]
                    ]
                ])
                writeFile file: 'healthz.json', text: healthzContent
            }

            stage('Python Test') {
                container('python') {
                    try {
                        sh """
                            pip install uv
                            uv sync --frozen --group dev
                            uv run coverage run -m pytest --maxfail=1 --disable-warnings -q --junitxml=testresults/results.xml
                            uv run coverage xml -o ${COVERAGE_REPORT}
                            test -f ${COVERAGE_REPORT}
                        """
                    } catch (e) {
                        sendFailureMessageOnSlack('Python tests failed')
                        throw e
                    }
                }
                junit skipPublishingChecks: true, testResults: '**/testresults/*.xml'
                stash includes: "${COVERAGE_REPORT}", name: 'coverage', allowEmpty: false
            }

            stage('Configure Azure Container Registry') {
                container('buildkit') {
                    withCredentials([string(credentialsId: "${CI_REGISTRY_ACCESS_KEY}", variable: 'CI_AUTH_BASE64')]) {
                        withCredentials([string(credentialsId: 'docker_hub_pull_token', variable: 'DOCKER_TOKEN')]) {
                            sh 'mkdir -p ~/.docker'
                            sh "echo \"{\\\"auths\\\": {\\\"${CI_REGISTRY}\\\": {\\\"auth\\\": \\\"${CI_AUTH_BASE64}\\\"}, \\\"${DOCKER_CONTAINER_REGISTRY_URL}\\\": {\\\"auth\\\": \\\"${DOCKER_TOKEN}\\\"}}}\" > ~/.docker/config.json"
                        }
                    }
                }
            }

            stage('Build Docker Images') {
                withCredentials([string(credentialsId: 'github_pat_nopermissions', variable: 'github_token')]) {
                    withCredentials([string(credentialsId: 'sonarqube_token', variable: 'sonarqube_token')]) {
                        // make a writable place to unstash
                        sh 'mkdir -p tmp_shared && chmod 777 tmp_shared'

                        dir('tmp_shared') {
                            unstash 'coverage'
                            sh "ls -la ${COVERAGE_REPORT}"
                        }
                        container('buildkit') {
                            def IMAGE = "${DOCKER_REGISTRY}${IMAGE_NAME}:${VERSION}"

                            try {
                                sh """
                                    cp tmp_shared/${COVERAGE_REPORT} ${DOCKER_FILE_PATH}/${COVERAGE_REPORT}
                                    buildctl build --frontend=dockerfile.v0 --local dockerfile=${DOCKER_FILE_PATH} --opt target=sonarqube --opt filename=Dockerfile --opt build-arg:SONAR_HOST=${SONARQUBE_HOST} --opt build-arg:SONAR_BRANCH=${BRANCH_NAME} --opt build-arg:SONAR_TOKEN=${sonarqube_token} --opt build-arg:SONAR_PROJECT_KEY=${SONARQUBE_PROJECT_NAME} --opt build-arg:COVERAGE_REPORT=${COVERAGE_REPORT} --local context=.
                                    buildctl build --frontend=dockerfile.v0 --local dockerfile=${DOCKER_FILE_PATH} --opt target=final --opt filename=Dockerfile --opt build-arg:APP_VERSION=${VERSION} --local context=. --output type=image,name=${IMAGE},push=true

                                    # Extract only the pre-built orderbook_core wheel (tiny stage, seconds not hours)
                                    buildctl build --frontend=dockerfile.v0 --local dockerfile=${DOCKER_FILE_PATH} --opt target=wheel-export --opt filename=Dockerfile --local context=. --output type=local,dest=rust-wheels
                                """
                            } catch (e) {
                                sendFailureMessageOnSlack('Docker build failed')
                                throw e
                            }
                            milestone(1)
                        }
                    }
                }
            }

            stage('Tag the release') {
                withCredentials([gitUsernamePassword(credentialsId: env.SCM_CRED_ID)]) {
                    sh "git tag v${VERSION} ${COMMITHASH}"
                    sh "git push origin v${VERSION}"
                }
            }

            stage('Update the GitOps repo') {
                dir('gitops-repo') {
                    checkout([$class: 'GitSCM', branches: [[name: '*/main']],
                              doGenerateSubmoduleConfigurations: false,
                              extensions: [[$class: 'LocalBranch', localBranch: '**']],
                              submoduleCfg: [],
                              userRemoteConfigs: [[credentialsId: env.SCM_CRED_ID,
                                                  url: GITOPS_REPO_URL]]])

                    withCredentials([gitUsernamePassword(credentialsId: env.SCM_CRED_ID)]) {
                        milestone(2)

                        def remoteBranchExists = sh(script: "git ls-remote --heads origin ${CONFIG_BRANCH_NAME}", returnStatus: true) == 0
                        if (remoteBranchExists) {
                            sh """
                                git config --global user.email 'devops@energyessentials.nl'
                                git config --global user.name 'Jenkins Build Agent'

                                git checkout ${CONFIG_BRANCH_NAME}

                                sed -i '/${GITOPS_MODULE_KEY}:/{n;n;s/version: .*/version: ${VERSION}/}' default.yaml.gotmpl

                                git add .
                                git commit -m "Bump ${GITOPS_MODULE_KEY} to version ${VERSION} based on commit ${COMMITHASH}"
                                git push --set-upstream origin ${CONFIG_BRANCH_NAME}

                                echo "Pushed new version to GitOps repository"
                            """
                        } else {
                            echo "Branch '${CONFIG_BRANCH_NAME}' could not be found in the vtx-config repository. Create the initial commit for this branch before the pipeline starts automatically updating the version."
                        }
                    }
                }
            }

            // sendSucceedMessageOnSlack()

        } catch (e) {
            echo "Pipeline failed: ${e.toString()}"
            throw e
        }
    }
}

properties([[
    $class: 'BuildDiscarderProperty',
    strategy: [
        $class: 'LogRotator',
        artifactDaysToKeepStr: '', artifactNumToKeepStr: '', daysToKeepStr: '', numToKeepStr: '50']
    ]
]);
